/**
 * 通用工具函数
 */

/**
 * 格式化日期为 YYYY-MM-DD
 */
function formatDate(d) {
  const date = d || new Date()
  const y = date.getFullYear()
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

/**
 * 格式化时间为 HH:MM
 */
function formatTime(date) {
  const h = String(date.getHours()).padStart(2, '0')
  const m = String(date.getMinutes()).padStart(2, '0')
  return `${h}:${m}`
}

/**
 * 获取有效日期（04:00 边界逻辑）
 */
function getEffectiveDate() {
  const now = new Date()
  if (now.getHours() < 4) {
    now.setDate(now.getDate() - 1)
  }
  return formatDate(now)
}

/**
 * 防抖
 */
function debounce(fn, delay = 300) {
  let timer = null
  return function (...args) {
    clearTimeout(timer)
    timer = setTimeout(() => fn.apply(this, args), delay)
  }
}

module.exports = {
  formatDate,
  formatTime,
  getEffectiveDate,
  debounce,
}
