<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { useQuery } from '@tanstack/vue-query'
import {
  Layers,
  MessageSquare,
  FolderOpen,
  ScanSearch,
  ArrowUpRight,
  Menu,
  BookOpen,
  Settings2,
  LogOut,
  UserRound,
} from 'lucide-vue-next'
import { api } from './api/client'
import KnowledgeBasePicker from './components/KnowledgeBasePicker.vue'
import LoginView from './views/LoginView.vue'
import ErrorBanner from './components/ErrorBanner.vue'
import { auth, bootstrap, isAdmin, logout, role, setupRequired } from './features/auth/store'
import { roleLabels } from './api/types'
const route = useRoute()
const mobile = ref(false)
onMounted(bootstrap)
const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30000 })
const section = computed(() =>
  route.path.startsWith('/knowledge')
    ? '知识库'
    : route.path.startsWith('/settings')
      ? '模型设置'
      : route.path.startsWith('/retrieval')
        ? '检索调试'
        : '问答工作台',
)
const navItems = [
  { path: '/chat', name: '问答工作台', icon: MessageSquare, hint: '01' },
  { path: '/knowledge', name: '知识库文件', icon: FolderOpen, hint: '02' },
  { path: '/retrieval', name: '检索调试', icon: ScanSearch, hint: '03' },
  { path: '/settings', name: '模型设置', icon: Settings2, hint: '04' },
]
const nav = computed(() => navItems.filter((item) => item.path !== '/settings' || isAdmin.value))
const roleName = computed(() => (role.value ? roleLabels[role.value] : '成员'))
async function signOut() {
  await logout()
}
</script>
<template>
  <div v-if="!auth.ready" class="boot-screen">
    <span class="dot online"></span>正在检查登录状态…
  </div>
  <div v-else-if="auth.error" class="boot-screen">
    <ErrorBanner :error="auth.error" />
    <button class="button" @click="bootstrap">重新连接</button>
  </div>
  <LoginView v-else-if="setupRequired || !auth.user" />
  <div v-else class="app-shell">
    <aside class="sidebar" :class="{ 'is-open': mobile }">
      <RouterLink to="/chat" class="brand" @click="mobile = false"
        ><span class="brand-mark"><Layers :size="23" /></span
        ><span>知序<span class="brand-sub">RAG WORKSPACE</span></span></RouterLink
      >
      <div class="workspace-label">个人工作空间 <span class="tiny-tag">WORKSPACE</span></div>
      <nav aria-label="主导航">
        <RouterLink
          v-for="item in nav"
          :key="item.path"
          :to="item.path"
          class="nav-item"
          :class="{ selected: route.path.startsWith(item.path) }"
          @click="mobile = false"
          ><component :is="item.icon" :size="18" /><span>{{ item.name }}</span
          ><small>{{ item.hint }}</small></RouterLink
        >
      </nav>
      <div class="sidebar-note">
        <span class="note-icon"><BookOpen :size="19" /></span><strong>知识，自有脉络。</strong>
        <p>把散落的文档，<br />变成触手可及的答案。</p>
        <RouterLink to="/knowledge" @click="mobile = false"
          >管理你的知识 <ArrowUpRight :size="14"
        /></RouterLink>
      </div>
      <div class="sidebar-footer">
        <span class="avatar"><UserRound :size="16" /></span>
        <div class="footer-copy">
          <strong>{{ auth.user.display_name }}</strong>
          <small>{{ roleName }}</small>
        </div>
        <button
          class="icon-button footer-logout"
          aria-label="退出登录"
          title="退出登录"
          @click="signOut"
        >
          <LogOut :size="16" />
        </button>
        <span class="dot" :class="{ online: health.isSuccess.value }"></span>
      </div>
    </aside>
    <button
      v-if="mobile"
      class="mobile-overlay"
      aria-label="关闭导航"
      @click="mobile = false"
    ></button>
    <div class="workspace-main">
      <header class="topbar">
        <div class="breadcrumbs">
          <button class="icon-button mobile-menu" aria-label="打开导航" @click="mobile = !mobile">
            <Menu :size="20" /></button
          ><span>工作空间</span><span class="slash">/</span><strong>{{ section }}</strong>
        </div>
        <div class="topbar-right">
          <span class="connection"
            ><span class="dot" :class="{ online: health.isSuccess.value }"></span
            >{{
              health.isError.value ? '服务未连接' : health.isPending.value ? '连接中' : '服务已连接'
            }}</span
          ><span class="topbar-divider"></span><KnowledgeBasePicker />
        </div>
      </header>
      <main>
        <RouterView v-slot="{ Component }">
          <Transition name="page" mode="out-in">
            <component :is="Component" :key="route.matched[0]?.path" />
          </Transition>
        </RouterView>
      </main>
    </div>
  </div>
</template>
<style scoped>
.boot-screen {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  color: var(--muted);
  font-size: 14px;
}
.footer-copy {
  min-width: 0;
  line-height: 1.25;
}
.footer-copy strong {
  display: block;
  font-size: 12px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.footer-copy small {
  color: #849f8d;
  font-size: 10px;
}
.footer-logout {
  margin-left: auto;
  color: #8fa690;
}
.footer-logout:hover {
  background: #263e32;
  color: #f0f4e9;
}
</style>
