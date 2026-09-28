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
					{
						type: 'number',
						id: 'speed',
						label: 'Speed (1.0-7.0)',
						min: 1,
						max: 7,
						step: 0.5,
						default: 2,
						isVisible: () => false,
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
					const x = Math.round(action.options.x * 100)
					const y = Math.round(action.options.y * 100)
					const r = await self.rpc('setTouchAFPosition', [x, y])
					self._lastResult.af = r
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
			focus_mode: {
				name: 'Set Focus Mode (camera/setFocusMode)',
				options: [
					{
						type: 'dropdown',
						id: 'mode',
						label: 'Mode',
						choices: [
							{ id: 'AF', label: 'AF (auto)' },
							{ id: 'MF', label: 'MF (manual)' },
						],
						default: 'AF',
					},
				],
				async callback(action) {
					const r = await self.rpc('setFocusMode', [action.options.mode], '1.1')
					self._lastResult.focusMode = r
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
				down: [{ actionId: 'zoom', options: { dir: 'in', state: 'start', speed: 2 } }],
				up: [{ actionId: 'zoom', options: { dir: 'in', state: 'stop' } }],
			}],
			[])

		mk('zoom_out', 'Zoom out (hold)', 'ZOOM-', blue,
			[{
				down: [{ actionId: 'zoom', options: { dir: 'out', state: 'start', speed: 1 } }],
				up: [{ actionId: 'zoom', options: { dir: 'out', state: 'stop' } }],
			}],
			[])

		mk('zoom_in_fast', 'Zoom in fast (hold)', 'ZOOM ++', blue,
			[{
				down: [{ actionId: 'zoom', options: { dir: 'in', state: 'start', speed: 5 } }],
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

		mk('af_cancel', 'AF cancel (back to wide AF)', 'AF\nWIDE', grey,
			[{ down: [{ actionId: 'touch_af_cancel', options: {} }], up: [] }],
			[])

		return presets
	}
	buildFeedbacks() {
		const self = this
		return {
			recording: {
				name: 'Recording state',
				type: 'boolean',
				description: 'Camera is currently recording',
				defaultStyle: { bgcolor: 0xff0000, fgcolor: 0xffffff },
				options: [],
				callback: () => this._recording === true,
			},
		}
	}

	buildVariables() {
		return {
			zoom_position: { name: 'zoom_position', label: 'Zoom position (index)' },
			recording: { name: 'recording', label: 'Recording (true/false)' },
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
				this.setVariableValues({
					zoom_position: String(pos),
					recording: recording ? 'true' : 'false',
					last_error: '',
				})
				this.checkFeedbacks('recording')
			} else if (r?.error) {
				this.setVariableValues({ last_error: String(r.error) })
			}
		}, 1000)
	}
}

export default SonyCamApi
