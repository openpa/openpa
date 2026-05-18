import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { watchEffect } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import hljsDarkUrl from 'highlight.js/styles/github-dark.css?url'
import hljsLightUrl from 'highlight.js/styles/github.css?url'
import './style.css'
import App from './App.vue'
import router from './router'
import { useSettingsStore } from './stores/settings'

document.title = __IS_ELECTRON__ ? 'OpenPA App' : 'OpenPA Web UI'

// Electron: leave the asar-bundled SPA snapshot in favor of the
// wheel-served SPA whenever a backend is reachable. The asar copy is
// frozen at electron-builder time, so wheel-only test releases ship
// new UI that this Electron shell never picks up otherwise (e.g. the
// About page added in v0.1.9-test19, invisible in Electron until the
// renderer leaves file://). Once we pivot, all future wheel updates
// auto-deliver their UI through the same HTTP origin.
//
// We deliberately stay on the asar in three situations:
//   - The web build (location.protocol !== 'file:') — no pivot needed.
//   - First-run install before the Setup Wizard has configured a
//     backend (bridge.config.agentUrl empty). The wizard itself runs
//     from the asar and configures the backend it'll then pivot to.
//   - Mid-Setup-Wizard (route under /setup). Navigating away here
//     would wipe in-flight form state.
async function maybePivotToBackend(): Promise<boolean> {
  if (window.location.protocol !== 'file:') return false
  const bridge = window.openpa
  if (!bridge) return false
  const agentUrl = bridge.config?.agentUrl
  if (!agentUrl || !/^https?:\/\//.test(agentUrl)) return false
  const route = window.location.hash.slice(1) || '/'
  if (route.startsWith('/setup')) return false
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 2000)
    const r = await fetch(`${agentUrl}/health`, { signal: controller.signal })
    clearTimeout(timer)
    if (!r.ok) return false
  } catch {
    return false
  }
  window.location.replace(`${agentUrl}/${window.location.hash || '#/'}`)
  return true
}

async function boot() {
  if (await maybePivotToBackend()) return  // navigation in flight; do not mount.

  const app = createApp(App)
  const pinia = createPinia()

  app.use(pinia)
  app.use(ElementPlus)
  app.use(router)

  app.mount('#app')

  // Apply theme attribute and highlight.js stylesheet reactively
  const settingsStore = useSettingsStore()

  function applyHljsTheme(theme: string) {
    const id = 'hljs-theme'
    let link = document.getElementById(id) as HTMLLinkElement | null
    if (!link) {
      link = document.createElement('link')
      link.id = id
      link.rel = 'stylesheet'
      document.head.appendChild(link)
    }
    link.href = theme === 'dark' ? hljsDarkUrl : hljsLightUrl
  }

  watchEffect(() => {
    document.documentElement.setAttribute('data-theme', settingsStore.theme)
    applyHljsTheme(settingsStore.theme)
  })
}

void boot()
