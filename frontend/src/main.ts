import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import { VueQueryPlugin, QueryClient } from '@tanstack/vue-query'
import PrimeVue from 'primevue/config'
import { zhixuPreset } from './ui/theme'
import App from './App.vue'
import ChatView from './views/ChatView.vue'
import KnowledgeView from './views/KnowledgeView.vue'
import RetrievalView from './views/RetrievalView.vue'
import SettingsView from './views/SettingsView.vue'
import './style.css'
const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/chat/:conversationId?', component: ChatView },
    { path: '/knowledge', component: KnowledgeView },
    { path: '/retrieval', component: RetrievalView },
    { path: '/settings', component: SettingsView },
    { path: '/:pathMatch(.*)*', redirect: '/chat' },
  ],
})
createApp(App)
  .use(router)
  .use(PrimeVue, {
    theme: {
      preset: zhixuPreset,
      options: { darkModeSelector: '.app-dark', cssLayer: false },
    },
    ripple: true,
  })
  .use(VueQueryPlugin, {
    queryClient: new QueryClient({
      defaultOptions: { queries: { retry: 1, staleTime: 3000, refetchOnWindowFocus: true } },
    }),
  })
  .mount('#app')
