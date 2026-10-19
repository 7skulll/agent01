<script setup lang="ts" generic="T extends string">
import { computed, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'
import { Check, ChevronDown, SlidersHorizontal } from 'lucide-vue-next'
const props = defineProps<{
  modelValue: T
  options: { value: T; label: string; description?: string }[]
  label: string
  disabled?: boolean
}>()
const emit = defineEmits<{ 'update:modelValue': [value: T] }>()
const root = ref<HTMLElement>()
const trigger = ref<HTMLButtonElement>()
const open = ref(false)
const active = ref(0)
const popoverLeft = ref(0)
const id = useId()
const selected = computed(() => props.options.find((option) => option.value === props.modelValue))
function show() {
  if (props.disabled || !props.options.length) return
  active.value = Math.max(
    0,
    props.options.findIndex((option) => option.value === props.modelValue),
  )
  const left = root.value?.getBoundingClientRect().left || 0
  const width = Math.min(270, window.innerWidth - 32)
  popoverLeft.value = Math.min(Math.max(16, left), window.innerWidth - width - 16) - left
  open.value = true
}
function choose(index: number) {
  const option = props.options[index]
  if (!option || props.disabled) return
  emit('update:modelValue', option.value)
  open.value = false
  trigger.value?.focus()
}
function keydown(event: KeyboardEvent) {
  if (props.disabled) return
  if (event.key === 'Tab') {
    open.value = false
    return
  }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter', ' ', 'Escape'].includes(event.key)) return
  if (event.key === 'Escape' && !open.value) return
  event.preventDefault()
  event.stopPropagation()
  if (event.key === 'Escape') {
    open.value = false
    trigger.value?.focus()
  } else if (!open.value) {
    show()
  } else if (event.key === 'Enter' || event.key === ' ') {
    choose(active.value)
  } else if (event.key === 'Home') {
    active.value = 0
  } else if (event.key === 'End') {
    active.value = props.options.length - 1
  } else {
    active.value =
      (active.value + (event.key === 'ArrowDown' ? 1 : -1) + props.options.length) %
      props.options.length
  }
}
function outside(event: PointerEvent) {
  if (!root.value?.contains(event.target as Node)) open.value = false
}
watch(
  () => props.disabled,
  (disabled) => {
    if (disabled) open.value = false
  },
)
function close() {
  open.value = false
}
onMounted(() => {
  document.addEventListener('pointerdown', outside)
  window.addEventListener('resize', close)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', outside)
  window.removeEventListener('resize', close)
})
</script>
<template>
  <div
    ref="root"
    class="select-menu"
    @keydown="keydown"
    @focusout="
      (event) => {
        if (!root?.contains(event.relatedTarget as Node)) open = false
      }
    "
  >
    <button
      ref="trigger"
      class="select-trigger"
      type="button"
      role="combobox"
      :aria-label="label"
      aria-haspopup="listbox"
      :aria-expanded="open"
      :aria-controls="`${id}-options`"
      :aria-activedescendant="open ? `${id}-option-${active}` : undefined"
      :disabled="disabled"
      @click="open ? (open = false) : show()"
    >
      <SlidersHorizontal :size="13" /><span>{{ selected?.label }}</span>
      <ChevronDown :size="13" class="select-chevron" :class="{ expanded: open }" />
    </button>
    <div
      v-if="open"
      :id="`${id}-options`"
      role="listbox"
      :aria-label="label"
      class="select-popover"
      :style="{ left: `${popoverLeft}px` }"
    >
      <div class="select-caption">{{ label }}</div>
      <button
        v-for="(option, index) in options"
        :id="`${id}-option-${index}`"
        :key="option.value"
        type="button"
        role="option"
        tabindex="-1"
        :aria-selected="modelValue === option.value"
        class="select-option"
        :class="{ active: active === index, chosen: modelValue === option.value }"
        @pointermove="active = index"
        @mousedown.prevent
        @click="choose(index)"
      >
        <span class="select-option-copy"
          ><strong>{{ option.label }}</strong
          ><small v-if="option.description">{{ option.description }}</small></span
        >
        <Check v-if="modelValue === option.value" :size="15" />
      </button>
    </div>
  </div>
</template>
