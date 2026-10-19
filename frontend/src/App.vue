<script setup lang="ts">
import { computed, ref } from 'vue'
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
  Command,
  Settings2,
} from 'lucide-vue-next'
import { api } from './api/client'
import KnowledgeBasePicker from './components/KnowledgeBasePicker.vue'
const route = useRoute()
const mobile = ref(false)
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
const nav = [
  { path: '/chat', name: '问答工作台', icon: MessageSquare, hint: '01' },
  { path: '/knowledge', name: '知识库文件', icon: FolderOpen, hint: '02' },
  { path: '/retrieval', name: '检索调试', icon: ScanSearch, hint: '03' },
  { path: '/settings', name: '模型设置', icon: Settings2, hint: '04' },
]
</script>
<template>
  <div class="app-shell">
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
        <span class="avatar"><Command :size="16" /></span>
        <div>知序工作空间<small>资料驱动 · 清晰有据</small></div>
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
      <main><RouterView /></main>
    </div>
  </div>
</template>
