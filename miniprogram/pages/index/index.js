/**
 * 心智同行 聊天主页
 * 核心功能：流式聊天 + 短句逐条发送 + 任务看板 + VIP 弹窗
 */

const app = getApp()
const api = require('../../utils/api')
const config = require('../../utils/config')

Page({
  data: {
    openid: '',
    messages: [],
    inputText: '',
    isStreaming: false,
    scrollTarget: '',
    showVipModal: false,
    isVip: false,
    loadingMore: false,
  },

  _requestTask: null,
  _streamBuffer: '',

  onLoad() {
    app.loginReady.then(() => {
      const openid = app.globalData.openid || 'dev_default'
      this.setData({ openid })
      this.loadAllMessages()
      this.loadProfile()
    })
  },

  onShow() {
    if (this.data.openid) {
      this.loadProfile()
      const taskBoard = this.selectComponent('#task-board')
      if (taskBoard) taskBoard.refresh()
    }
  },

  async loadProfile() {
    try {
      const profile = await api.get(`/api/user/profile/${this.data.openid}`)
      this.setData({ isVip: profile.is_vip })
      app.globalData.isVip = profile.is_vip
    } catch (e) {
      console.error('[Index] 加载用户信息失败', e)
    }
  },

  async loadAllMessages() {
    try {
      const msgs = await api.get(`/api/chat/all_messages/${this.data.openid}`)
      const processed = this._processMessages(msgs)
      this.setData({ messages: processed })
      this.scrollToBottom()
    } catch (e) {
      console.error('[Index] 加载消息失败', e)
    }
  },

  async loadOlderMessages() {
    if (this.data.loadingMore || this.data.messages.length === 0) return
    const firstMsg = this.data.messages.find(m => m.id)
    if (!firstMsg) return

    this.setData({ loadingMore: true })
    try {
      const msgs = await api.get(`/api/chat/all_messages/${this.data.openid}?before_id=${firstMsg.id}`)
      if (msgs.length === 0) return
      const processed = this._processMessages(msgs)
      this.setData({
        messages: [...processed, ...this.data.messages],
        loadingMore: false,
      })
    } catch (e) {
      this.setData({ loadingMore: false })
    }
  },

  _processMessages(msgs) {
    let lastDate = ''
    return msgs.map(m => {
      const item = {
        id: m.id,
        role: m.role,
        content: m.content,
        timeLabel: m.created_at ? m.created_at.slice(11, 16) : '',
        showDateMarker: false,
        dateLabel: '',
      }
      const convDate = m.conversation_date || ''
      if (convDate !== lastDate) {
        item.showDateMarker = true
        item.dateLabel = this.formatDateLabel(convDate)
        lastDate = convDate
      }
      return item
    })
  },

  formatDateLabel(dateStr) {
    const today = new Date()
    const d = new Date(dateStr + 'T00:00:00')
    const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`
    if (dateStr === todayStr) return '今天'
    const yesterday = new Date(today)
    yesterday.setDate(yesterday.getDate() - 1)
    const yesterdayStr = `${yesterday.getFullYear()}-${String(yesterday.getMonth() + 1).padStart(2, '0')}-${String(yesterday.getDate()).padStart(2, '0')}`
    if (dateStr === yesterdayStr) return '昨天'
    return `${d.getMonth() + 1}月${d.getDate()}日`
  },

  onInput(e) {
    this.setData({ inputText: e.detail.value })
  },

  onSend() {
    const text = this.data.inputText.trim()
    if (!text || this.data.isStreaming) return

    const userMsg = {
      id: 'temp_' + Date.now(),
      role: 'user',
      content: text,
      showDateMarker: false,
      dateLabel: '',
      timeLabel: new Date().toTimeString().slice(0, 5),
    }
    this.setData({
      messages: [...this.data.messages, userMsg],
      inputText: '',
      isStreaming: true,
    })
    this._streamBuffer = ''
    this.scrollToBottom()

    this._requestTask = api.streamChat(
      this.data.openid,
      text,
      (chunk) => {
        this._streamBuffer += chunk
      },
      () => {
        this._sendShortMessages(this._streamBuffer)
      }
    )

    this.requestNotificationAuth()
  },

  _sendShortMessages(fullText) {
    if (!fullText) {
      this.setData({ isStreaming: false })
      return
    }

    const sentences = this._splitIntoSentences(fullText)
    const timeLabel = new Date().toTimeString().slice(0, 5)
    let delay = 0

    sentences.forEach((sentence, index) => {
      delay += 300 + Math.random() * 400
      setTimeout(() => {
        const shortMsg = {
          id: 'temp_ai_' + Date.now() + '_' + index,
          role: 'assistant',
          content: sentence,
          showDateMarker: false,
          dateLabel: '',
          timeLabel: index === sentences.length - 1 ? timeLabel : '',
        }
        this.setData({
          messages: [...this.data.messages, shortMsg],
        })
        this.scrollToBottom()

        if (index === sentences.length - 1) {
          this.setData({ isStreaming: false })
          const taskBoard = this.selectComponent('#task-board')
          if (taskBoard) taskBoard.refresh()
        }
      }, delay)
    })
  },

  _splitIntoSentences(text) {
    // Remove TASK_SPLIT markers
    text = text.replace(/\|\|\|TASK_SPLIT:\{.*?\}\|\|\|/g, '')

    // Split by newlines first (LLM is instructed to use newlines)
    let lines = text.split(/\n+/).map(l => l.trim()).filter(l => l.length > 0)

    // Further split long lines by sentence-ending punctuation
    let result = []
    for (const line of lines) {
      if (line.length <= 20) {
        result.push(line)
        continue
      }
      // Split by 。！？ and keep the punctuation
      let parts = line.split(/(?<=[。！？…])/g)
      let current = ''
      for (const part of parts) {
        current += part
        if (current.length >= 4) {
          result.push(current.trim())
          current = ''
        }
      }
      if (current.trim()) result.push(current.trim())
    }

    return result.length > 0 ? result : [text.trim()]
  },

  scrollToBottom() {
    setTimeout(() => {
      this.setData({ scrollTarget: 'scroll-bottom' })
    }, 50)
  },

  onSettingsTap() {
    wx.navigateTo({ url: `/pages/settings/settings?openid=${this.data.openid}` })
  },

  onAdminTrigger() {
    wx.navigateTo({ url: `/pages/admin/admin?openid=${this.data.openid}` })
  },

  onUpgradeVip() {
    if (this.data.isVip) {
      wx.showToast({ title: '你已经是VIP了', icon: 'none' })
      return
    }
    this.createPayOrder()
  },

  async createPayOrder() {
    try {
      const params = await api.post('/api/pay/create_order', {
        openid: this.data.openid,
      })

      this.setData({ showVipModal: false })

      wx.requestPayment({
        timeStamp: params.time_stamp,
        nonceStr: params.nonce_str,
        package: params.package,
        signType: params.sign_type,
        paySign: params.pay_sign,
        success: () => {
          wx.showToast({ title: '支付成功，确认中...', icon: 'none' })
          this.pollVipStatus()
        },
        fail: (err) => {
          if (err.errMsg !== 'requestPayment:fail cancel') {
            wx.showToast({ title: '支付失败', icon: 'none' })
          }
        }
      })
    } catch (e) {
      console.error('[Index] 下单失败', e)
      wx.showToast({ title: '下单失败', icon: 'none' })
    }
  },

  showVipPrompt() {
    if (this.data.isVip) return false
    this.setData({ showVipModal: true })
    return true
  },

  closeVipModal() {
    this.setData({ showVipModal: false })
  },

  requestNotificationAuth() {
    if (app.globalData.isDevMode) return
    const templateId = config.SUBSCRIBE_TEMPLATE_ID
    if (!templateId) return
    wx.requestSubscribeMessage({
      tmplIds: [templateId],
      success: (res) => {
        if (res[templateId] === 'accept') {
          api.post('/api/user/subscribe_auth', {
            openid: this.data.openid,
            template_id: templateId,
          })
        }
      },
      fail: () => {}
    })
  },

  async pollVipStatus(maxRetries = 5) {
    for (let i = 0; i < maxRetries; i++) {
      await new Promise(r => setTimeout(r, 2000))
      try {
        const res = await api.get(`/api/pay/status/${this.data.openid}`)
        if (res.is_vip) {
          this.setData({ isVip: true })
          app.globalData.isVip = true
          wx.showToast({ title: 'VIP已生效！', icon: 'success' })
          return
        }
      } catch (e) {}
    }
    wx.showToast({ title: '支付确认中，请稍后查看', icon: 'none' })
  },

  onUnload() {
    if (this._requestTask) {
      this._requestTask.abort()
    }
  }
})
