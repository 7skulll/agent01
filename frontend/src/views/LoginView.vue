<script setup lang="ts">
import { computed, ref } from 'vue'
import InputText from 'primevue/inputtext'
import FloatLabel from 'primevue/floatlabel'
import Password from 'primevue/password'
import Button from 'primevue/button'
import { Layers, LogIn, ShieldCheck, UserPlus, UserRound } from 'lucide-vue-next'
import {
  auth,
  login,
  register,
  registrationEnabled,
  setup,
  setupRequired,
} from '../features/auth/store'
import ErrorBanner from '../components/ErrorBanner.vue'
import { roleDescriptions, roleLabels, type Role } from '../api/types'

const username = ref('')
const password = ref('')
const confirm = ref('')
const displayName = ref('')
const busy = ref(false)
const error = ref<unknown>(null)
const registering = ref(false)
const setupMode = computed(() => setupRequired.value)
const registerMode = computed(() => !setupMode.value && registering.value)
const needConfirm = computed(() => setupMode.value || registerMode.value)
const minLength = computed(() => auth.status?.password_min_length ?? 8)
const mismatch = computed(() => needConfirm.value && confirm.value !== password.value)
const canSubmit = computed(
  () =>
    !!username.value.trim() &&
    password.value.length >= (needConfirm.value ? minLength.value : 1) &&
    !mismatch.value,
)
const roles = Object.keys(roleLabels) as Role[]
function toggle() {
  registering.value = !registering.value
  error.value = null
  password.value = ''
  confirm.value = ''
}
async function submit() {
  if (busy.value || !canSubmit.value) return
  busy.value = true
  error.value = null
  try {
    if (setupMode.value) await setup(username.value, password.value, displayName.value)
    else if (registerMode.value) await register(username.value, password.value, displayName.value)
    else await login(username.value, password.value)
  } catch (e) {
    error.value = e
  } finally {
    busy.value = false
  }
}
</script>
<template>
  <div class="auth-screen">
    <section class="auth-card zx-card-rise">
      <div class="auth-brand">
        <span class="brand-mark"><Layers :size="22" /></span>
        <div><strong>知序</strong><small>RAG WORKSPACE</small></div>
      </div>
      <template v-if="setupMode">
        <span class="subtle-chip"><ShieldCheck :size="14" /> 首次初始化</span>
        <h1>创建管理员账号</h1>
        <p class="auth-intro">
          系统还没有任何账号。创建管理员后，登录功能立即生效；之后可在「用户管理」中添加员工或普通用户。
        </p>
      </template>
      <template v-else-if="registerMode">
        <span class="subtle-chip"><UserPlus :size="14" /> 注册账号</span>
        <h1>注册普通用户</h1>
        <p class="auth-intro">
          注册后即可问答、检索和查看资料。上传文件与管理知识库需要管理员将账号升级为员工。
        </p>
      </template>
      <template v-else>
        <span class="subtle-chip"><LogIn :size="14" /> 账号登录</span>
        <h1>登录工作空间</h1>
        <p class="auth-intro">使用管理员或同事分配的账号登录。会话过期后需要重新登录。</p>
      </template>

      <form class="auth-form" @submit.prevent="submit">
        <FloatLabel variant="on" class="auth-field zx-rise" style="--zx-i: 0">
          <InputText id="auth-username" v-model.trim="username" autocomplete="username" />
          <label for="auth-username">用户名</label>
        </FloatLabel>
        <FloatLabel v-if="needConfirm" variant="on" class="auth-field zx-rise" style="--zx-i: 1">
          <InputText id="auth-display" v-model.trim="displayName" autocomplete="name" />
          <label for="auth-display">显示名称（可留空）</label>
        </FloatLabel>
        <FloatLabel variant="on" class="auth-field zx-rise" style="--zx-i: 2">
          <Password
            v-model="password"
            input-id="auth-password"
            :feedback="false"
            :toggle-mask="true"
            :invalid="needConfirm && password.length > 0 && password.length < minLength"
            :placeholder="needConfirm ? `至少 ${minLength} 位` : '请输入密码'"
            :autocomplete="needConfirm ? 'new-password' : 'current-password'"
            aria-label="密码"
          />
          <label for="auth-password">密码</label>
        </FloatLabel>
        <FloatLabel v-if="needConfirm" variant="on" class="auth-field zx-rise" style="--zx-i: 3">
          <Password
            v-model="confirm"
            input-id="auth-confirm"
            :feedback="false"
            :toggle-mask="true"
            :invalid="!!confirm && mismatch"
            placeholder="再次输入密码"
            autocomplete="new-password"
          />
          <label for="auth-confirm">确认密码</label>
        </FloatLabel>
        <p v-if="mismatch" class="auth-hint">两次输入的密码不一致。</p>
        <ErrorBanner :error="error" />
        <Button
          type="submit"
          class="auth-submit"
          :label="setupMode ? '创建管理员并进入' : registerMode ? '注册并进入' : '登录'"
          :loading="busy"
          :disabled="!canSubmit"
        />
      </form>

      <div v-if="setupMode" class="auth-roles zx-rise" style="--zx-i: 4">
        <p>角色权限说明</p>
        <ul>
          <li v-for="item in roles" :key="item">
            <span class="badge" :class="item === 'admin' ? 'completed' : 'muted'">{{
              roleLabels[item]
            }}</span>
            <small>{{ roleDescriptions[item] }}</small>
          </li>
        </ul>
      </div>
      <div v-else class="auth-switch zx-rise" style="--zx-i: 4">
        <Button v-if="registerMode" link size="small" label="已有账号？返回登录" @click="toggle" />
        <Button
          v-else-if="registrationEnabled"
          link
          size="small"
          label="还没有账号？注册普通用户"
          @click="toggle"
        />
        <p v-else class="auth-footnote">
          <UserRound :size="13" /> 自助注册已关闭，请联系管理员开通账号。
        </p>
      </div>
      <p v-if="!setupMode && !registerMode" class="auth-footnote zx-rise" style="--zx-i: 5">
        <UserRound :size="13" /> 忘记密码请联系管理员重置；管理员可在用户管理中停用账号。
      </p>
    </section>
  </div>
</template>
<style scoped>
.auth-screen {
  min-height: 100vh;
  display: grid;
  place-items: center;
  padding: 48px 20px;
  background:
    radial-gradient(1100px 500px at 15% -10%, rgba(47, 125, 82, 0.16), transparent 60%),
    radial-gradient(900px 480px at 90% 110%, rgba(23, 58, 94, 0.14), transparent 55%), #f4f6f1;
}
.auth-card {
  width: min(430px, 100%);
  padding: 36px 36px 26px;
  display: grid;
  gap: 14px;
  border: 1px solid rgb(255 255 255 / 65%);
  border-radius: 20px;
  background: rgb(255 255 255 / 82%);
  backdrop-filter: blur(14px);
  box-shadow:
    0 24px 60px -28px rgb(20 46 32 / 35%),
    0 2px 8px rgb(20 46 32 / 6%);
}
.auth-brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.auth-brand strong {
  display: block;
  font-size: 19px;
  letter-spacing: 0.1em;
}
.auth-brand small {
  color: var(--muted);
  font-size: 10px;
  letter-spacing: 0.24em;
}
.auth-card h1 {
  margin: 2px 0 0;
  font-size: 27px;
  letter-spacing: -0.02em;
}
.auth-intro {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.75;
}
.auth-form {
  display: grid;
  gap: 18px;
  margin-top: 4px;
}
.auth-field {
  display: block;
}
.auth-field :deep(.p-inputtext),
.auth-field :deep(.p-password),
.auth-field :deep(.p-password input) {
  width: 100%;
}
.auth-hint {
  margin: -6px 0 0;
  color: #b3423a;
  font-size: 12px;
}
.auth-submit {
  justify-content: center;
  margin-top: 2px;
  height: 44px;
  font-size: 14px;
}
.auth-roles {
  border-top: 1px dashed var(--line);
  padding-top: 14px;
  display: grid;
  gap: 8px;
}
.auth-roles p {
  margin: 0;
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.12em;
}
.auth-roles ul {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  gap: 9px;
}
.auth-roles li {
  display: grid;
  grid-template-columns: 76px 1fr;
  align-items: center;
  gap: 10px;
}
.auth-roles small {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}
.auth-switch {
  display: flex;
  justify-content: center;
}
.auth-footnote {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 2px 0 0;
  color: var(--muted);
  font-size: 12px;
}
</style>
