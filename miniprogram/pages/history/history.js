/**
 * 历史记录页 — 展示对话历史并支持切换
 */

const api = require('../../utils/api')
const util = require('../../utils/util')

Page({
  data: {
    openid: '',
    conversations: [],
    currentDate: '',
  },

  onLoad(options) {
    this.setData({
      openid: options.openid || '',
      currentDate: util.getEffectiveDate(),
    })
    this.loadHistory()
  },

  async loadHistory() {
    if (!this.data.openid) return
    try {
      const conversations = await api.get(`/api/chat/history/${this.data.openid}`)
      this.setData({ conversations })
    } catch (e) {
      console.error('[History] 加载失败', e)
    }
  },

  onSelectConversation(e) {
    const { date } = e.currentTarget.dataset
    // 返回聊天页并切换到该日期
    const pages = getCurrentPages()
    const chatPage = pages[pages.length - 2]
    if (chatPage && chatPage.switchDate) {
      chatPage.switchDate(date)
    }
    wx.navigateBack()
  },

  onPullDownRefresh() {
    this.loadHistory().then(() => wx.stopPullDownRefresh())
  }
})
