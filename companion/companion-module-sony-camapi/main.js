/* Sony Camera Remote API (ScalarWebAPI v1) module for Bitfocus Companion.
 * Endpoint: http://<camera>:10000/ (auto-cached from camapi.py discovery or
 * entered manually). JSON-RPC POST per Sony's public Camera Remote API spec.
 */
const { InstanceBase, InstanceStatus, runEntrypoint } = require('@companion-module/base')
const http = require('http')
const url = require('url')

class SonyCamApi extends InstanceBase {
	constructor(internal) {
		super(internal)
		this._pollTimer = null
		this._lastResult = {}
	}

	async configFields() {
		return [
			{
				type: 'textinput',
				id: 'endpoint',
				label: 'API endpoint URL',
				tooltip: 'Full URL, e.g. http://192.168.5.10:10000/ — see camapi.py discover output',
				width: 12,
				default: 'http://192.168.5.10:10000/',
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
		this.setActionDefinitions(this.buildActions())
		this.setFeedbackDefinitions(this.buildFeedbacks())
		this.setVariableDefinitions(this.buildVariables())
		this.setPresetDefinitions(this.buildPresets())
		this.updateStatus(InstanceStatus.Ok)
		this.startPolling()
	}

	async destroy() {
		if (this._pollTimer) clearInterval(this._pollTimer)
	}

	getConfigFields() {
		// companion v1.x shim
		return this.configFields ? undefined : undefined
	}

	rpc(method, params, version = '1.0') {
		return new Promise((resolve) => {
			const ep = (this.config.endpoint || '').replace(/\/?$/, '/')
			const full = /\/camera\/?$/.test(ep) ? ep : ep + 'camera'
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
			req.on('error', (e) => resolve({ error: e.message }))
			req.on('timeout', () => req.destroy(new Error('timeout')))
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
						default: 1,
					},
				],
				async callback(action) {
					const speed = action.options.state === 'stop' ? undefined : String(action.options.speed)
					const params = speed
						? [action.options.dir, action.options.state, speed]
						: [action.options.dir, action.options.state]
					const r = await self.rpc('actZoom', params)
					self._lastResult.zoom = r
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

	buildPresets() {
		const self = this
		return {
			zoom_in: {
				name: 'Zoom in (hold)',
				category: 'Zoom',
				type: 'button',
				style: { text: 'ZOOM+', size: '18', bgcolor: 0x114477 },
				feedbacks: [],
				steps: [
					{
						down: [{ actionId: 'zoom', options: { dir: 'in', state: 'start', speed: 2 } }],
						up: [{ actionId: 'zoom', options: { dir: 'in', state: 'stop' } }],
					},
				],
			},
			zoom_out: {
				name: 'Zoom out (hold)',
				type: 'button',
				style: { text: 'ZOOM-', size: '18', bgcolor: 0x114477 },
				feedbacks: [],
				steps: [
					{
						down: [{ actionId: 'zoom', options: { dir: 'out', state: 'start', speed: 1 } }],
						up: [{ actionId: 'zoom', options: { dir: 'out', state: 'stop' } }],
					},
				],
			},
			zoom_in_fast: {
				name: 'Zoom in fast (hold)',
				type: 'button',
				style: { text: 'ZOOM+', bgcolor: 0x2266aa },
				steps: [
					{
						down: [{ actionId: 'zoom', options: { dir: 'in', state: 'start', speed: 5 } }],
						up: [{ actionId: 'zoom', options: { dir: 'in', state: 'stop' } }],
					},
				],
			},
			rec: {
				name: 'REC start/stop (red when recording)',
				type: 'button',
				style: { text: 'REC', size: '18', bgcolor: 0x800000 },
				feedbacks: [
					{
						feedbackId: 'recording',
						style: { bgcolor: 0xff0000, fgcolor: 0xffffff },
					},
				],
				steps: [{ down: [{ actionId: 'rec_toggle', options: {} }], up: [] }],
			},
		}
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
		return [
			{ name: 'zoom_position', label: 'Zoom position (index)' },
			{ name: 'recording', label: 'Recording (true/false)' },
			{ name: 'last_error', label: 'Last API error' },
		]
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

runEntrypoint(SonyCamApi, [])
