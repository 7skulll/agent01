<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import FloatLabel from 'primevue/floatlabel'
import Password from 'primevue/password'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import { api, date } from '../../api/client'
import ActionDialog from '../../components/ActionDialog.vue'
import ErrorBanner from '../../components/ErrorBanner.vue'
import { auth, refreshSession } from '../auth/store'
import { roleDescriptions, roleLabels, type AuthUser, type Role } from '../../api/types'

const client = useQueryClient()
const users = useQuery({ queryKey: ['users'], queryFn: api.users })
const roles = Object.keys(roleLabels) as Role[]
const roleOptions = roles.map((value) => ({ value, label: roleLabels[value] }))
const minLength = computed(() => auth.status?.password_min_length ?? 8)

const creating = ref(false)
const form = reactive({ username: '', display_name: '', role: 'viewer' as Role, password: '' })
const busy = ref('')
const error = ref<unknown>(null)
const message = ref('')
const resetting = ref<AuthUser | null>(null)
const resetPassword = ref('')
const removing = ref<AuthUser | null>(null)

async function replace(user: AuthUser) {
  client.setQueryData<AuthUser[]>(['users'], (old) =>
    (old || []).map((item) => (item.user_id === user.user_id ? user : item)),
  )
  if (user.user_id === auth.user?.user_id) {
    // 管理员修改了自己的角色：立即同步导航与权限，避免继续显示旧入口。
    try {
      await refreshSession()
    } catch {
      /* 会话失效时由全局 401 处理 */
    }
  }
}
async function create() {
  if (busy.value) return
  busy.value = 'create'
  error.value = null
  message.value = ''
  try {
    const user = await api.createUser({ ...form })
    client.setQueryData<AuthUser[]>(['users'], (old) => [...(old || []), user])
    message.value = `已创建 ${user.display_name}（${roleLabels[user.role as Role]}）。`
    creating.value = false
    Object.assign(form, { username: '', display_name: '', role: 'viewer', password: '' })
  } catch (e) {
    error.value = e
  } finally {
    busy.value = ''
  }
}
async function changeRole(user: AuthUser, role: Role) {
  if (busy.value || user.role === role) return
  busy.value = `role:${user.user_id}`
  error.value = null
  try {
    await replace(await api.updateUser(user.user_id, { role }))
  } catch (e) {
    error.value = e
  } finally {
    busy.value = ''
  }
}
async function toggle(user: AuthUser) {
  if (busy.value) return
  busy.value = `active:${user.user_id}`
  error.value = null
  try {
    await replace(await api.updateUser(user.user_id, { is_active: !user.is_active }))
  } catch (e) {
    error.value = e
  } finally {
    busy.value = ''
  }
}
function openReset(user: AuthUser) {
  resetting.value = user
  resetPassword.value = ''
  error.value = null
}
async function savePassword() {
  if (!resetting.value || busy.value) return
  busy.value = 'reset'
  error.value = null
  try {
    await api.updateUser(resetting.value.user_id, { password: resetPassword.value })
    message.value = `已重置 ${resetting.value.display_name} 的密码，该账号的现有登录已失效。`
    resetting.value = null
    resetPassword.value = ''
  } catch (e) {
    error.value = e
  } finally {
    busy.value = ''
  }
}
async function remove() {
  if (!removing.value || busy.value) return
  busy.value = 'remove'
  error.value = null
  try {
    await api.deleteUser(removing.value.user_id)
    client.setQueryData<AuthUser[]>(['users'], (old) =>
      (old || []).filter((item) => item.user_id !== removing.value?.user_id),
    )
    message.value = '用户已删除，其登录会话同时失效。'
    removing.value = null
  } catch (e) {
    error.value = e
  } finally {
    busy.value = ''
  }
}
</script>
<template>
  <section class="card users-card">
    <div class="section-heading">
      <div class="section-title">
        <h2>用户与权限</h2>
        <span class="count">{{ users.data.value?.length ?? 0 }}</span>
      </div>
      <Button v-if="!creating" icon="pi pi-user-plus" label="新建用户" @click="creating = true" />
    </div>
    <p class="users-intro">
      普通用户只能问答、检索和查看资料；员工可上传文件、构建索引并管理知识库；管理员额外拥有用户管理与模型配置权限。
    </p>
    <ErrorBanner :error="error || users.error.value" />
    <div v-if="message" class="notice" role="status">{{ message }}</div>

    <form v-if="creating" class="users-create" @submit.prevent="create">
      <FloatLabel variant="on" class="users-field"
        ><InputText
          id="user-username"
          v-model.trim="form.username"
          autocomplete="off"
          maxlength="32"
        /><label for="user-username">用户名（3–32 位）</label></FloatLabel
      >
      <FloatLabel variant="on" class="users-field"
        ><InputText id="user-display" v-model.trim="form.display_name" maxlength="64" /><label
          for="user-display"
          >显示名称（可留空）</label
        ></FloatLabel
      >
      <span class="users-field"
        ><Select
          v-model="form.role"
          :options="roleOptions"
          option-label="label"
          option-value="value"
          input-id="user-role"
          aria-label="角色"
      /></span>
      <span class="users-field"
        ><Password
          v-model="form.password"
          input-id="user-password"
          :feedback="false"
          toggle-mask
          :placeholder="`初始密码 · 至少 ${minLength} 位`"
          autocomplete="new-password"
      /></span>
      <p class="users-role-hint">{{ roleDescriptions[form.role] }}</p>
      <div class="users-create-actions">
        <Button
          type="button"
          severity="secondary"
          variant="outlined"
          label="取消"
          :disabled="!!busy"
          @click="creating = false"
        /><Button type="submit" label="创建用户" :loading="busy === 'create'" />
      </div>
    </form>

    <div v-if="users.isPending.value" class="muted">正在读取用户…</div>
    <div v-else class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>用户</th>
            <th>角色</th>
            <th>状态</th>
            <th>最近登录</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="user in users.data.value" :key="user.user_id">
            <td>
              <div class="user-cell">
                <span class="file-icon"><UserRound :size="17" /></span>
                <div>
                  <strong>{{ user.display_name }}</strong>
                  <small>@{{ user.username }}</small>
                </div>
                <Tag v-if="user.user_id === auth.user?.user_id" severity="secondary" value="本人" />
                <Tag v-if="user.service" severity="secondary" value="服务令牌" />
              </div>
            </td>
            <td>
              <Select
                class="role-select"
                :model-value="user.role"
                :options="roleOptions"
                option-label="label"
                option-value="value"
                :aria-label="`${user.display_name} 的角色`"
                :disabled="!!busy"
                @update:model-value="changeRole(user, $event as Role)"
              />
            </td>
            <td>
              <Tag
                :severity="user.is_active ? 'success' : 'danger'"
                :value="user.is_active ? '启用' : '已停用'"
              />
            </td>
            <td class="nowrap">{{ user.last_login_at ? date(user.last_login_at) : '从未登录' }}</td>
            <td>
              <div class="file-actions">
                <Button
                  text
                  size="small"
                  label="重置密码"
                  :disabled="!!busy"
                  @click="openReset(user)"
                />
                <Button
                  text
                  size="small"
                  :label="user.is_active ? '停用' : '启用'"
                  :disabled="!!busy || user.user_id === auth.user?.user_id"
                  @click="toggle(user)"
                />
                <Button
                  text
                  severity="danger"
                  size="small"
                  label="删除"
                  :disabled="!!busy || user.user_id === auth.user?.user_id"
                  @click="removing = user"
                />
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="settings-footnote">
      密码使用 scrypt
      加盐摘要保存；停用、改密或删除会立即失效该账号的全部登录会话。必须至少保留一个启用状态的管理员。
    </p>

    <ActionDialog
      :open="!!resetting"
      title="重置密码"
      :busy="busy === 'reset'"
      @close="resetting = null"
    >
      <p>为「{{ resetting?.display_name }}」设置新密码，该账号所有已登录会话会立即失效。</p>
      <span class="users-field"
        ><Password
          v-model="resetPassword"
          input-id="reset-password"
          :feedback="false"
          toggle-mask
          :placeholder="`新密码 · 至少 ${minLength} 位`"
          autocomplete="new-password"
      /></span>
      <ErrorBanner :error="error" />
      <div class="kb-modal-actions">
        <Button
          severity="secondary"
          variant="outlined"
          label="取消"
          :disabled="busy === 'reset'"
          @click="resetting = null"
        /><Button
          label="确认重置"
          :loading="busy === 'reset'"
          :disabled="resetPassword.length < minLength"
          @click="savePassword"
        />
      </div>
    </ActionDialog>
    <ActionDialog
      :open="!!removing"
      title="删除用户"
      :busy="busy === 'remove'"
      @close="removing = null"
    >
      <p>确认删除「{{ removing?.display_name }}」（@{{ removing?.username }}）？此操作无法撤销。</p>
      <p class="muted">该用户的登录会话立即失效，历史问答内容仍会保留。</p>
      <ErrorBanner :error="error" />
      <div class="kb-modal-actions">
        <Button
          severity="secondary"
          variant="outlined"
          label="取消"
          :disabled="busy === 'remove'"
          @click="removing = null"
        /><Button
          severity="danger"
          :label="busy === 'remove' ? '删除中…' : '确认删除用户'"
          :loading="busy === 'remove'"
          @click="remove"
        />
      </div>
    </ActionDialog>
  </section>
</template>
<style scoped>
.users-card {
  padding: 24px 26px;
}
.users-intro {
  margin: 6px 0 16px;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.7;
  max-width: 760px;
}
.users-create {
  display: grid;
  grid-template-columns: repeat(4, minmax(140px, 1fr));
  gap: 12px;
  padding: 16px;
  margin-bottom: 18px;
  border: 1px dashed var(--line);
  border-radius: 12px;
}
.users-role-hint {
  grid-column: 1 / -1;
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}
.users-create-actions {
  grid-column: 1 / -1;
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
.user-cell {
  display: flex;
  align-items: center;
  gap: 10px;
}
.user-cell small {
  display: block;
  color: var(--muted);
  font-size: 12px;
}
.role-select {
  max-width: 130px;
}
.users-field {
  display: block;
}
.users-field :deep(.p-inputtext),
.users-field :deep(.p-password),
.users-field :deep(.p-password input),
.users-field :deep(.p-select) {
  width: 100%;
}
@media (max-width: 860px) {
  .users-create {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
