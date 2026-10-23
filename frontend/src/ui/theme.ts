import { definePreset } from '@primeuix/themes'
import Aura from '@primeuix/themes/aura'

/**
 * 知序主题：在 Aura 基础上收敛为「深松绿 + 暖纸白」的工作台配色。
 * 组件层（style.css）中的 --accent 等令牌与此保持一致。
 */
export const zhixuPreset = definePreset(Aura, {
  semantic: {
    primary: {
      50: '#eef6ee',
      100: '#d8ebd9',
      200: '#b3d8b6',
      300: '#88c08d',
      400: '#5da466',
      500: '#2f7d52',
      600: '#286a46',
      700: '#22573a',
      800: '#1c452f',
      900: '#163425',
      950: '#0d2018',
    },
    colorScheme: {
      light: {
        surface: {
          0: '#ffffff',
          50: '#f7f8f5',
          100: '#eef0ea',
          200: '#e2e6de',
          300: '#cfd6ca',
          400: '#a8b3a0',
          500: '#84937c',
          600: '#5f7358',
          700: '#43573f',
          800: '#2c3d2c',
          900: '#1c2a1f',
          950: '#101a14',
        },
        primary: {
          color: '{primary.600}',
          hoverColor: '{primary.700}',
          activeColor: '{primary.800}',
        },
        highlight: {
          background: '{surface.100}',
          focusBackground: '{primary.100}',
          color: '{surface.900}',
          focusColor: '{primary.950}',
        },
      },
    },
  },
})
