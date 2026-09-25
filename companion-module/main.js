import { InstanceBase, InstanceStatus, combineRgb } from '@companion-module/base'

const BASE_DEFAULT = 'http://127.0.0.1:8787'

class LancBridgeInstance extends InstanceBase {
	constructor(internal) {
		super(internal)
		this.state = {}
		this.timer = null
	}

	async init(config) {
		this.config = config
		this.updateStatus(InstanceStatus.Connecting)
		this.setActionDefinitions(this.getActionDefinitions())
		this.setFeedbackDefinitions(this.getFeedbackDefinitions())
		this.setPresets()
		this.startPolling()
	}

	getConfigFields() {
		return [
			{
				type: 'textinput',
				id: 'base',
				label: 'lancbridge base URL',
				tooltip: 'Where lanc_gpio.py listens',
				default: BASE_DEFAULT,
				width: 8,
			},
		]
	}

	async destroy() {
		if (this.timer) clearInterval(this.timer)
	}

	async apiGet(path) {
		const res = await fetch((this.config.base || BASE_DEFAULT) + path)
		if (!res.ok) throw new Error('HTTP ' + res.status)
		return res.json()
	}

	startPolling() {
		if (this.timer) clearInterval(this.timer)
		this.timer = setInterval(async () => {
			try {
				const j = await this.apiGet('/status')
				this.state = j || {}
				this.updateStatus(this.state.connected ? InstanceStatus.Ok : InstanceStatus.UnknownError, this.state.connected ? undefined : 'LANC not connected')
				this.checkFeedbacks(...['recording', 'connected'])
			} catch (e) {
				this.updateStatus(InstanceStatus.UnknownError, 'lancbridge unreachable: ' + e.message)
			}
		}, 1000)
	}

	async send(path) {
		try {
			await this.apiGet(path)
			this.updateStatus(InstanceStatus.Ok)
		} catch (e) {
			this.log('error', 'lancbridge request failed: ' + e.message)
		}
	}

	getActionDefinitions() {
		const dir = (label) => ({
			type: 'dropdown',
			label,
			id: 'dir',
			choices: [
				{ id: 'in', label: 'In' },
				{ id: 'out', label: 'Out' },
			],
			default: 'in',
		})
		const speed = {
			type: 'dropdown',
			label: 'Speed',
			id: 'speed',
			choices: [
				{ id: 'slow', label: 'Slow' },
				{ id: 'fast', label: 'Fast' },
			],
			default: 'slow',
		}
		const hold = (name, path, dirs, withSpeed) => ({
			name,
			options: withSpeed ? [dir(dirs), speed] : [dir(dirs)],
			callback: async (action) => {
				const qs = new URLSearchParams({ dir: action.options.dir })
				if (withSpeed && action.options.speed === 'fast') qs.set('speed', 'fast')
				qs.set('state', 'on')
				await this.send(`${path}?${qs.toString()}`)
			},
		})
		return {
			rec: { name: 'REC start/stop', options: [], callback: () => this.send('/rec') },
			stop: { name: 'Stop zoom/focus/iris', options: [], callback: () => this.send('/stop') },
			zoom: hold('Zoom (hold to move)', '/zoom', 'Zoom direction', true),
			focus: hold('Focus (hold to move)', '/focus', 'Focus direction', false),
			iris: hold('Iris (hold to move)', '/iris', 'Iris direction', false),
			aftoggle: { name: 'AF on/off toggle', options: [], callback: () => this.send('/aftoggle') },
			display: { name: 'Data screen', options: [], callback: () => this.send('/display') },
			poweroff: { name: 'Power off camera', options: [], callback: () => this.send('/poweroff') },
		}
	}

	getFeedbackDefinitions() {
		return {
			recording: {
				type: 'boolean',
				name: 'Camera is recording',
				description: 'Lights while the camera records',
				options: [],
				callback: () => !!this.state.recording,
			},
			connected: {
				type: 'boolean',
				name: 'LANC is connected',
				description: 'Lights while the camera LANC link is up',
				options: [],
				callback: () => !!this.state.connected,
			},
		}
	}

	setPresets() {
		const presets = {}
		const mk = (id, name, text, bgcolor, steps, feedbacks) => {
			presets[id] = {
				name,
				type: 'simple',
				keywords: ['lanc', 'camera', 'sony'],
				style: { text, size: 'auto', color: combineRgb(255, 255, 255), bgcolor },
				steps,
				feedbacks,
			}
		}

		const grey = combineRgb(45, 45, 45)
		const red = combineRgb(180, 30, 30)
		const green = combineRgb(30, 120, 30)
		const blue = combineRgb(30, 60, 160)
		const dark = combineRgb(120, 20, 20)

		const recFeedback = [{ feedbackId: 'recording', options: {}, style: { bgcolor: dark, color: combineRgb(255, 255, 255) } }]

		// REC toggle — red while recording
		mk('rec', 'REC start/stop', 'REC', grey,
			[{ down: [{ actionId: 'rec', options: {} }], up: [] }],
			recFeedback)

		// Zoom in / out — hold-to-move (state=on down, state=off up)
		const zoomPreset = (dir, label) =>
			mk(`zoom_${dir}`, `Zoom ${dir} (hold)`, label, grey,
				[{
					down: [{ actionId: 'zoom', options: { dir, speed: 'slow' } }],
					up: [{ actionId: 'stop', options: {} }],
				}],
				[])
		zoomPreset('in', 'ZOOM +')
		zoomPreset('out', 'ZOOM −')

		// Fast zooms
		mk('zoom_in_fast', 'Zoom in fast (hold)', 'ZOOM ++', grey,
			[{
				down: [{ actionId: 'zoom', options: { dir: 'in', speed: 'fast' } }],
				up: [{ actionId: 'zoom', options: { dir: 'in', speed: 'fast' } }],
			}],
			[])
		mk('zoom_out_fast', 'Zoom out fast (hold)', 'ZOOM −−', grey,
			[{
				down: [{ actionId: 'zoom', options: { dir: 'out', speed: 'fast' } }],
				up: [{ actionId: 'zoom', options: { dir: 'out', speed: 'fast' } }],
			}],
			[])

		// Focus near / far — hold
		const focusPreset = (dir, label) =>
			mk(`focus_${dir}`, `Focus ${dir} (hold)`, label, grey,
				[{
					down: [{ actionId: 'focus', options: { dir } }],
					up: [{ actionId: 'stop', options: {} }],
				}],
				[])
		focusPreset('near', 'FOC ⊕')
		focusPreset('far', 'FOC ⊖')

		// Iris open / close — hold
		const irisPreset = (dir, label) =>
			mk(`iris_${dir}`, `Iris ${dir} (hold)`, label, grey,
				[{
					down: [{ actionId: 'iris', options: { dir } }],
					up: [{ actionId: 'stop', options: {} }],
				}],
				[])
		irisPreset('open', 'IRIS +')
		irisPreset('close', 'IRIS −')

		// AF toggle (blue while LANC connected)
		mk('af', 'AF on/off toggle', 'AF', grey,
			[{ down: [{ actionId: 'aftoggle', options: {} }], up: [] }],
			[{ feedbackId: 'connected', options: {}, style: { bgcolor: blue } }])

		// Data screen
		mk('display', 'Data screen', 'DATA', grey,
			[{ down: [{ actionId: 'display', options: {} }], up: [] }], [])

		// Power off (deliberately dark — destructive)
		mk('poweroff', 'Power off camera', 'PWR ⏻', dark,
			[{ down: [{ actionId: 'poweroff', options: {} }], up: [] }], [])

		this.setPresetDefinitions(
			[
				{
					id: 'lancbridge_main',
					name: 'Sony HXR-MC2500 (LANC)',
					description: 'Camera control via lancbridge, with REC tally',
					definitions: Object.keys(presets),
				},
			],
			presets,
		)
	}
}

export default LancBridgeInstance