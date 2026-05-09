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
