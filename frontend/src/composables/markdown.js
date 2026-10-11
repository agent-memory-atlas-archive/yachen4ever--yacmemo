// 笔记内容来自任意 MCP agent（可写库），v-html 直灌 marked 产物等于把管理
// 界面交给被投毒的 agent：<img onerror> / javascript: 链接都会执行，而该源
// 持有会话 cookie、能读 /api/config。marked v14 起不再提供 sanitize 选项，
// 统一在出口过 DOMPurify（2026-10-11 审计 P1）。所有 v-html 渲染必须走这里。
import { marked } from 'marked'
import DOMPurify from 'dompurify'

export function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text || ''))
}
