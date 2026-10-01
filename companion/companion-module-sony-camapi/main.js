/* Sony Camera Remote API (ScalarWebAPI v1) module for Bitfocus Companion.
 * Endpoint: http://<camera>:10000/ (auto-cached from camapi.py discovery or
 * entered manually). JSON-RPC POST per Sony's public Camera Remote API spec.
 */
import { InstanceBase, InstanceStatus, combineRgb } from '@companion-module/base'
import http from 'http'
import url from 'url'

class SonyCamApi extends InstanceBase {
	constructor(internal) {
		super(internal)
		this._pollTimer = null
		this._lastResult = {}
	}

	getConfigFields() {
		return [
			{
				type: 'textinput',
				id: 'endpoint',
				label: 'API endpoint URL',
				tooltip: 'Full URL, e.g. http://192.168.5.10:10000/ — see camapi.py discover output',
				width: 12,
				default: 'http://192.168.122.1:10000/sony/camera',
				regex: '/^https?:\\/\\//',
			},
			{
				type: 'checkbox',
				id: 'poll',
				label: 'Poll camera status (feedbacks + variables)',
				default: true,
				width: 12,
			},
		]
	}

	async init(config) {
		this.config = config
		// 2014-era MC2500 remote session starts in still mode; switch to movie
		// once at init so zoom/AF operate immediately
		this.rpc('setShootMode', ['movie']).then((r) => {
			this.log('info', 'setShootMode(movie): ' + JSON.stringify(r))
		})
		this.setActionDefinitions(this.buildActions())
		this.setFeedbackDefinitions(this.buildFeedbacks())
		this.setVariableDefinitions(this.buildVariables())
		this.setPresetDefinitions(this.buildPresetCategories(), this.buildPresets())
		this.updateStatus(InstanceStatus.Ok)
		this.startPolling()
	}

	async destroy() {
		if (this._pollTimer) clearInterval(this._pollTimer)
	}

	rpc(method, params, version = '1.0') {
		return new Promise((resolve) => {
			// Accept a bare base URL or a full one; the MC2500 always serves /sony/camera
			let ep = (this.config.endpoint || '').trim().replace(/\/+$/, '')
			if (/\/camera$/.test(ep)) {
				// already a full endpoint (…/camera or …/sony/camera)
			} else {
				ep = ep + '/sony/camera'
			}
			const full = ep
			const body = JSON.stringify({ method, params: params || [], id: 1, version })
			const u = new url.URL(full)
			const req = http.request(
				{
					hostname: u.hostname,
					port: u.port || 80,
					path: u.pathname,
					method: 'POST',
					headers: { 'Content-Type': 'application/json' },
					timeout: 8000,
				},
				(res) => {
					let data = ''
					res.on('data', (c) => (data += c))
					res.on('end', () => {
						try {
							resolve(JSON.parse(data))
						} catch {
							resolve({ error: 'bad json', raw: data })
						}
					})
				}
			)
			req.on('error', (e) => {
				this.log('warn', method + ' transport error: ' + e.message)
				resolve({ error: e.message })
			})
			req.on('timeout', () => req.destroy(new Error('timeout')))
			this.log('debug', '-> ' + method + ' ' + full + ' ' + body)
			req.write(body)
			req.end()
		})
	}

	buildActions() {
		const self = this
		return {
			zoom: {
				name: 'Zoom',
				options: [
					{
						type: 'dropdown',
						id: 'dir',
						label: 'Direction',
						choices: [
							{ id: 'in', label: 'Tele (in)' },
							{ id: 'out', label: 'Wide (out)' },
						],
						default: 'in',
					},
					{
						type: 'dropdown',
						id: 'state',
						label: 'Action',
						choices: [
							{ id: 'start', label: 'Start (hold)' },
							{ id: 'stop', label: 'Stop' },
							{ id: 'onepush', label: 'One push (fixed step)' },
						],
						default: 'start',
					},
									],
				async callback(action) {
					// 2014-era MC2500 firmware: actZoom takes exactly 2 params
					const r = await self.rpc('actZoom', [action.options.dir, action.options.state])
					self._lastResult.zoom = r
					self.log('info', 'actZoom(' + action.options.dir + ',' + action.options.state + ') -> ' + JSON.stringify(r))
				},
			},
			rec: {
				name: 'Record Start/Stop',
				options: [
					{
						type: 'dropdown',
						id: 'action',
						label: 'Action',
						choices: [
							{ id: 'start', label: 'Start' },
							{ id: 'stop', label: 'Stop' },
						],
						default: 'start',
					},
				],
				async callback(action) {
					const r = await self.rpc(action.options.action === 'start' ? 'startMovieRec' : 'stopMovieRec')
					self._lastResult.rec = r
					self.checkFeedbacks('recording')
				},
			},
			rec_toggle: {
				name: 'Record Toggle',
				options: [],
				async callback() {
					const st = await self.rpc('getEvent', [false])
					const ev = st?.result?.[0] || {}
					const recording = ev.movieRecording === 'recording' || ev.cameraFunction === 'Movie'
					const r = await self.rpc(recording ? 'stopMovieRec' : 'startMovieRec')
					self._lastResult.rec = r
					self.checkFeedbacks('recording')
				},
			},
			touch_af: {
				name: 'Touch AF (set focus point)',
				options: [
					{
						type: 'number',
						id: 'x',
						label: 'X position (%)',
						min: 0,
						max: 100,
						step: 1,
						default: 50,
					},
					{
						type: 'number',
						id: 'y',
						label: 'Y position (%)',
						min: 0,
						max: 100,
						step: 1,
						default: 50,
					},
				],
				async callback(action) {
					// Sony expects coordinates in 0-100 units (getMethodTypes: ["double","double"])
					const x = action.options.x
					const y = action.options.y
					const r = await self.rpc('setTouchAFPosition', [x, y])
					self._lastResult.af = r
					const afRes = r?.result?.[1]?.AFResult
					this._afOk = afRes === true
					if (afRes !== undefined) self.checkFeedbacks('af_result')
				},
			},
			touch_af_cancel: {
				name: 'Touch AF Cancel',
				options: [],
				async callback() {
					const r = await self.rpc('cancelTouchAFPosition')
					self._lastResult.af = r
				},
			},
			liveview: {
				name: 'Liveview Start/Stop',
				options: [
					{
						type: 'dropdown',
						id: 'action',
						label: 'Action',
						choices: [
							{ id: 'start', label: 'Start' },
							{ id: 'stop', label: 'Stop' },
						],
						default: 'start',
					},
				],
				async callback(action) {
					const m = action.options.action === 'start' ? 'startLiveview' : 'stopLiveview'
					const r = await self.rpc(m)
					self._lastResult.liveview = r
				},
			},
			shoot_mode: {
				name: 'Set Shoot Mode',
				options: [
					{
						type: 'dropdown',
						id: 'mode',
						label: 'Mode',
						// getAvailableShootMode on the MC2500 reports still/movie
						choices: [
							{ id: 'movie', label: 'Movie' },
							{ id: 'still', label: 'Still' },
						],
						default: 'movie',
					},
				],
				async callback(action) {
					const r = await self.rpc('setShootMode', [action.options.mode])
					self._lastResult.shootMode = r
					self.checkFeedbacks('recording')
				},
			},
			method: {
				name: 'Raw API Call',
				options: [
					{
						type: 'textinput',
						id: 'method',
						label: 'Method name',
						default: 'getVersions',
					},
					{
						type: 'textinput',
						id: 'params',
						label: 'Params (JSON array)',
						default: '[]',
					},
				],
				async callback(action) {
					let params = []
					try {
						params = JSON.parse(action.options.params || '[]')
					} catch {
						params = []
					}
					const r = await self.rpc(action.options.method, params)
					self._lastResult.raw = r
					self.log('info', action.options.method + ' -> ' + JSON.stringify(r))
				},
			},
			apis: {
				name: 'Get Available API List (log)',
				options: [],
				async callback() {
					const r = await self.rpc('getAvailableApiList')
					self.log('info', 'Available APIs: ' + JSON.stringify(r?.result?.[0] || r))
				},
			},
		}
	}

	buildPresetCategories() {
		return [
			{
				id: 'sony_cam',
				name: 'Sony HXR-MC2500 (WiFi)',
				description: 'Zoom and AF control via Camera Remote API',
				definitions: [
					{
						id: 'sony_cam_controls',
						type: 'simple',
						name: 'Camera controls',
						description: 'Zoom in/out, AF presets',
						presets: [
							'zoom_in',
							'zoom_out',
							'zoom_in_fast',
							'rec',
							'af_center',
							'af_left',
							'af_right',
							'af_closeup',
							'af_cancel',
						],
					},
					{
						id: 'sony_cam_modes',
						type: 'simple',
						name: 'Camera modes & status',
						description: 'Shoot mode, liveview, aperture, raw call',
						presets: [
							'mode_movie',
							'mode_still',
							'liveview_toggle',
							'status_dump',
							'raw_call',
						],
					},
				],
			},
		]
	}

	buildPresets() {
		const presets = {}
		const mk = (id, name, text, bgcolor, steps, feedbacks) => {
			presets[id] = {
				name,
				type: 'simple',
				keywords: ['sony', 'camera', 'zoom'],
				style: { text, size: 'auto', color: combineRgb(255, 255, 255), bgcolor },
				steps,
				feedbacks,
			}
		}

		const grey = combineRgb(45, 45, 45)
		const blue = combineRgb(30, 60, 160)
		const green = combineRgb(30, 120, 30)
		const darkred = combineRgb(120, 20, 20)

		mk('zoom_in', 'Zoom in (hold)', 'ZOOM+', blue,
			[{
				down: [{ actionId: 'zoom', options: { dir: 'in', state: 'start' } }],
				up: [{ actionId: 'zoom', options: { dir: 'in', state: 'stop' } }],
			}],
			[])

		mk('zoom_out', 'Zoom out (hold)', 'ZOOM-', blue,
			[{
				down: [{ actionId: 'zoom', options: { dir: 'out', state: 'start' } }],
				up: [{ actionId: 'zoom', options: { dir: 'out', state: 'stop' } }],
			}],
			[])

		mk('zoom_in_fast', 'Zoom in fast (hold)', 'ZOOM ++', blue,
			[{
				down: [{ actionId: 'zoom', options: { dir: 'in', state: 'start' } }],
				up: [{ actionId: 'zoom', options: { dir: 'in', state: 'stop' } }],
			}],
			[])

		mk('rec', 'REC start/stop', 'REC', darkred,
			[{ down: [{ actionId: 'rec_toggle', options: {} }], up: [] }],
			[{ feedbackId: 'recording', options: {}, style: { bgcolor: combineRgb(255, 0, 0), color: combineRgb(255, 255, 255) } }])

		mk('af_center', 'AF center point', 'AF\nCEN', green,
			[{ down: [{ actionId: 'touch_af', options: { x: 50, y: 50 } }], up: [] }],
			[])

		mk('af_left', 'AF left third point', 'AF\nL', green,
			[{ down: [{ actionId: 'touch_af', options: { x: 25, y: 50 } }], up: [] }],
			[])

		mk('af_right', 'AF right third point', 'AF\nR', green,
			[{ down: [{ actionId: 'touch_af', options: { x: 75, y: 50 } }], up: [] }],
			[])

		mk('af_closeup', 'AF close-up (lower center)', 'AF\nCLOSE', green,
			[{ down: [{ actionId: 'touch_af', options: { x: 50, y: 65 } }], up: [] }],
			[])

		mk('af_cancel', 'AF cancel (back to wide AF)', 'AF\\nWIDE', grey,
			[{ down: [{ actionId: 'touch_af_cancel', options: {} }], up: [] }],
			[])

		// --- modes & status presets ---
		mk('mode_movie', 'Shoot mode: Movie', 'MODE\\nMOVIE', blue,
			[{ down: [{ actionId: 'shoot_mode', options: { mode: 'movie' } }], up: [] }],
			[])

		mk('mode_still', 'Shoot mode: Still', 'MODE\\nSTILL', blue,
			[{ down: [{ actionId: 'shoot_mode', options: { mode: 'still' } }], up: [] }],
			[])

		presets['liveview_toggle'] = {
			name: 'Liveview start/stop (shows live state)',
			type: 'simple',
			keywords: ['sony', 'camera', 'liveview'],
			style: { text: 'LV', size: 'auto', color: combineRgb(255, 255, 255), bgcolor: grey },
			steps: [{
				down: [
					{ actionId: 'liveview', options: { action: 'start' } },
				],
				up: [
					{ actionId: 'liveview', options: { action: 'stop' } },
				],
			}],
			feedbacks: [{
				feedbackId: 'liveview_on',
				options: {},
				style: { bgcolor: combineRgb(30, 120, 30), color: combineRgb(255, 255, 255) },
			}],
		}

		presets['status_dump'] = {
			name: 'Dump camera status to log',
			type: 'simple',
			keywords: ['sony', 'camera', 'status'],
			style: { text: 'STATUS', size: 'auto', color: combineRgb(255, 255, 255), bgcolor: grey },
			steps: [{ down: [{ actionId: 'apis', options: {} }], up: [] }],
			feedbacks: [],
		}

		presets['raw_call'] = {
			name: 'Raw API call (configure in editor)',
			type: 'simple',
			keywords: ['sony', 'camera', 'api'],
			style: { text: 'API', size: 'auto', color: combineRgb(255, 255, 255), bgcolor: grey },
			steps: [{ down: [{ actionId: 'method', options: { method: 'getVersions', params: '[]' } }], up: [] }],
			feedbacks: [],
		}

		return presets
	}
	buildFeedbacks() {
		const self = this
		return {
			recording: {
				name: 'Recording state',
				type: 'boolean',
				description: 'Camera is currently recording',
				defaultStyle: { bgcolor: combineRgb(255, 0, 0), fgcolor: combineRgb(255, 255, 255) },
				options: [],
				callback: () => this._recording === true,
			},
			liveview_on: {
				name: 'Liveview active',
				type: 'boolean',
				description: 'Camera liveview stream is running',
				defaultStyle: { bgcolor: combineRgb(30, 120, 30), fgcolor: combineRgb(255, 255, 255) },
				options: [],
				callback: () => this._liveview === true,
			},
			af_result: {
				name: 'Touch AF result',
				type: 'boolean',
				description: 'Last touch-AF attempt succeeded',
				defaultStyle: { bgcolor: combineRgb(30, 120, 30), fgcolor: combineRgb(255, 255, 255) },
				options: [],
				callback: () => this._afOk === true,
			},
		}
	}

	buildVariables() {
		return {
			zoom_position: { name: 'zoom_position', label: 'Zoom position (index)' },
			recording: { name: 'recording', label: 'Recording (true/false)' },
			liveview: { name: 'liveview', label: 'Liveview (true/false)' },
			shoot_mode: { name: 'shoot_mode', label: 'Current shoot mode' },
			f_number: { name: 'f_number', label: 'Current f-number' },
			white_balance: { name: 'white_balance', label: 'White balance mode (from getEvent)' },
			camera_status: { name: 'camera_status', label: 'Camera status (from getEvent)' },
			storage_info: { name: 'storage_info', label: 'Storage summary' },
			last_error: { name: 'last_error', label: 'Last API error' },
		}
	}

	startPolling() {
		if (this._pollTimer) clearInterval(this._pollTimer)
		if (!this.config.poll) return
		this._pollTimer = setInterval(async () => {
			const r = await this.rpc('getEvent', [false])
			if (r?.result?.[0]) {
				const ev = r.result[0]
				const zi = ev.zoomInformation || {}
				const pos = zi.zoomPosition ?? zi.zoomIndexCurrentBox ?? ''
				const recording =
					ev.movieRecording === 'recording' ||
					ev.cameraFunction === 'Movie' ||
					ev.status === 'recording'
				this._recording = recording
				this._liveview = ev.liveviewStatus === true
				const storage = Array.isArray(ev.storageInformation)
					? ev.storageInformation
							.map((s) => `${s.storageDescription || s.storageID}: ${s.recordableTime ?? '?'}s`)
							.join(' | ')
					: ''
				this.setVariableValues({
					zoom_position: String(pos),
					recording: recording ? 'true' : 'false',
					liveview: this._liveview ? 'true' : 'false',
					shoot_mode: String(ev.currentShootMode ?? ev.shootMode ?? ''),
					f_number: String(ev.currentFNumber ?? ''),
					white_balance: String(ev.currentWhiteBalanceMode ?? ev.whiteBalance ?? ''),
					camera_status: String(ev.cameraStatus ?? ''),
					storage_info: storage,
					last_error: '',
				})
				this.checkFeedbacks('recording', 'liveview_on', 'af_result')
			} else if (r?.error) {
				this.setVariableValues({ last_error: String(r.error) })
			}
		}, 1000)
	}
}

export default SonyCamApi
