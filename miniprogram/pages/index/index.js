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
    inputLineCount: 1,
    isStreaming: false,
    scrollTarget: '',
    showVipModal: false,
    showVipReasonModal: false,
    isVip: false,
    loadingMore: false,
    coachAvatar: '',
    coachAvatarIsImage: false,
    vipReasonOptions: [
      { label: '价格原因', value: 'price' },
      { label: '还没看出价值', value: 'unclear_value' },
      { label: '怕被打扰', value: 'afraid_disturb' },
      { label: '先试用看看', value: 'try_first' },
    ],
  },

  _requestTask: null,
  _streamBuffer: '',
  _pendingTaskChanges: [],

  onLoad() {
    const storedAvatar = wx.getStorageSync('mindmate_coach_avatar')
    const storedAvatarIsImage = wx.getStorageSync('mindmate_coach_avatar_is_image') === true
    this.setData({
      coachAvatar: storedAvatarIsImage ? storedAvatar : '',
      coachAvatarIsImage: storedAvatarIsImage && !!storedAvatar,
    })
    app.loginReady.then((loginSucceeded) => {
      if (!loginSucceeded || !app.globalData.openid) return
      const openid = app.globalData.openid
      this.setData({ openid })
      this.loadAllMessages()
      this.loadProfile()
    })
  },

  async onShow() {
    if (this.data.openid) {
      await this.loadProfile()
      const taskBoard = this.selectComponent('#task-board')
      if (taskBoard) taskBoard.refresh()
      // 从设置页跳回时自动弹出VIP弹窗
      if (app.globalData.showVipOnShow) {
        delete app.globalData.showVipOnShow
        if (!this.data.isVip) {
          this.setData({ showVipModal: true })
        }
      }
    }
  },

  async loadProfile() {
    try {
      const paymentStatus = await api.get(`/api/pay/status/${this.data.openid}`)
      const profile = await api.get(`/api/user/profile/${this.data.openid}`)
      const isVip = paymentStatus.is_vip || profile.is_vip
      this.setData({
        isVip,
        showVipModal: isVip ? false : this.data.showVipModal,
        showVipReasonModal: isVip ? false : this.data.showVipReasonModal,
      })
      app.globalData.isVip = isVip
      app.globalData.vipStatusReady = true
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
    // 只使用真实数据库ID，过滤掉临时消息ID（temp_开头）
    const firstRealMsg = this.data.messages.find(m => m.id && !String(m.id).startsWith('temp_'))
    if (!firstRealMsg) return

    this.setData({ loadingMore: true })
    try {
      const msgs = await api.get(`/api/chat/all_messages/${this.data.openid}?before_id=${firstRealMsg.id}`)
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
        feedbackSubmitted: false,
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

  onInputLineChange(e) {
    const lineCount = Math.max(1, Number(e.detail.lineCount) || 1)
    if (lineCount !== this.data.inputLineCount) {
      this.setData({ inputLineCount: lineCount })
    }
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
      inputLineCount: 1,
      isStreaming: true,
    })
    this._streamBuffer = ''
    this._pendingTaskChanges = []
    this.scrollToBottom()
    this.trackEvent('chat_send', { length: text.length })

    this._requestTask = api.streamChat(
      this.data.openid,
      text,
      (chunk) => {
        this._streamBuffer += chunk
      },
      () => {
        this._sendShortMessages(this._streamBuffer)
      },
      (changes) => {
        this._pendingTaskChanges = changes
      }
    )

    this.requestNotificationAuth('behavior_nudge')
  },

  _sendShortMessages(fullText) {
    if (!fullText) {
      this.setData({ isStreaming: false })
      const taskBoard = this.selectComponent('#task-board')
      if (taskBoard) taskBoard.refreshWithChanges(this._pendingTaskChanges)
      this._pendingTaskChanges = []
      return
    }

    const sentences = this._splitIntoSentences(fullText)
    if (sentences.length === 0) {
      this.setData({ isStreaming: false })
      const taskBoard = this.selectComponent('#task-board')
      if (taskBoard) taskBoard.refreshWithChanges(this._pendingTaskChanges)
      this._pendingTaskChanges = []
      return
    }

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
          feedbackSubmitted: false,
        }
        this.setData({
          messages: [...this.data.messages, shortMsg],
        })
        this.scrollToBottom()

        if (index === sentences.length - 1) {
          this.setData({ isStreaming: false })
          // 延迟500ms确保后端标记处理完成，再刷新任务看板
          setTimeout(() => {
            const taskBoard = this.selectComponent('#task-board')
            if (taskBoard) taskBoard.refreshWithChanges(this._pendingTaskChanges)
            this._pendingTaskChanges = []
          }, 500)
        }
      }, delay)
    })
  },

  _splitIntoSentences(text) {
    // 清除所有任务标记（通用正则，覆盖所有 TASK_xxx 类型）
    text = text.replace(/\|\|\|TASK_\w+:[^|]*\|\|\|/g, '')
    if (!text.trim()) return []

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

  onAvatarTap() {
    wx.chooseMedia({
      count: 1,
      mediaType: ['image'],
      sourceType: ['album', 'camera'],
      sizeType: ['compressed'],
      success: (res) => {
        const tempFilePath = res.tempFiles[0] && res.tempFiles[0].tempFilePath
        if (!tempFilePath) return
        wx.saveFile({
          tempFilePath,
          success: (saved) => this._saveCoachAvatar(saved.savedFilePath),
          fail: () => this._saveCoachAvatar(tempFilePath),
        })
      },
    })
  },

  _saveCoachAvatar(path) {
    wx.setStorageSync('mindmate_coach_avatar', path)
    wx.setStorageSync('mindmate_coach_avatar_is_image', true)
    this.setData({ coachAvatar: path, coachAvatarIsImage: true })
  },

  onAdminTrigger() {
    wx.navigateTo({ url: `/pages/admin/admin?openid=${this.data.openid}` })
  },

  onUpgradeVip() {
    if (this.data.isVip) {
      wx.showToast({ title: '你已经是VIP了', icon: 'none' })
      return
    }
    this.trackEvent('vip_upgrade_click', { source: 'index_modal' })
    this.createPayOrder()
  },

  async createPayOrder() {
    try {
      const params = await api.post('/api/pay/create_order', {
        openid: this.data.openid,
      })
      this.trackEvent('pay_order_created', { out_trade_no: params.out_trade_no || '' })

      this.setData({ showVipModal: false })

      wx.requestPayment({
        timeStamp: params.time_stamp,
        nonceStr: params.nonce_str,
        package: params.package,
        signType: params.sign_type,
        paySign: params.pay_sign,
        success: () => {
          this.trackEvent('pay_success', { out_trade_no: params.out_trade_no || '' })
          wx.showToast({ title: '支付成功，确认中...', icon: 'none' })
          this.pollVipStatus()
          this.requestNotificationAuth('vip_followup')
        },
        fail: (err) => {
          if (err.errMsg === 'requestPayment:fail cancel') {
            this.trackEvent('pay_cancel', { out_trade_no: params.out_trade_no || '' })
            this.markPayCancelled(params.out_trade_no)
          } else {
            this.trackEvent('pay_fail', { out_trade_no: params.out_trade_no || '', errMsg: err.errMsg || '' })
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
    this.trackEvent('vip_modal_view', { source: 'nav' })
    return true
  },

  closeVipModal() {
    if (!this.data.isVip) {
      this.trackEvent('vip_modal_dismiss', { source: 'index_modal' })
      this.setData({ showVipModal: false, showVipReasonModal: true })
      return
    }
    this.setData({ showVipModal: false })
  },

  closeVipReasonModal() {
    this.setData({ showVipReasonModal: false })
  },

  onVipReasonTap(e) {
    const reason = e.currentTarget.dataset.value
    this.trackEvent('vip_modal_reason', { reason })
    api.post('/api/feedback', {
      openid: this.data.openid,
      target_type: 'vip_modal',
      target_id: 'index',
      rating: reason,
    }).catch(() => {})
    this.setData({ showVipReasonModal: false })
  },

  requestNotificationAuth(scene = 'general') {
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
            scene,
          })
          this.trackEvent('subscribe_auth_accept', { scene })
        }
      },
      fail: () => {
        this.trackEvent('subscribe_auth_fail', { scene })
      }
    })
  },

  async pollVipStatus(maxRetries = 5) {
    for (let i = 0; i < maxRetries; i++) {
      await new Promise(r => setTimeout(r, 2000))
      try {
        const res = await api.get(`/api/pay/status/${this.data.openid}`)
        if (res.is_vip) {
          this.setData({ isVip: true, showVipModal: false, showVipReasonModal: false })
          app.globalData.isVip = true
          app.globalData.vipStatusReady = true
          this.trackEvent('vip_activated', {
            out_trade_no: res.latest_out_trade_no || '',
          })
          wx.showToast({ title: 'VIP已生效！', icon: 'success' })
          return
        }
      } catch (e) {}
    }
    wx.showToast({ title: '支付确认中，请稍后查看', icon: 'none' })
  },

  markPayCancelled(outTradeNo) {
    if (!outTradeNo) return
    api.post('/api/pay/cancel', {
      openid: this.data.openid,
      out_trade_no: outTradeNo,
    }).catch(() => {})
  },

  onUnload() {
    if (this._requestTask) {
      this._requestTask.abort()
    }
  },

  onFeedbackTap(e) {
    const { index, id, rating } = e.currentTarget.dataset
    this.setData({ [`messages[${index}].feedbackSubmitted`]: true })
    api.post('/api/feedback', {
      openid: this.data.openid,
      target_type: 'chat',
      target_id: String(id || ''),
      rating,
    }).then(() => {
      this.trackEvent('feedback_submit', { target_type: 'chat', rating })
      wx.showToast({ title: '已收到', icon: 'none' })
    }).catch(() => {
      wx.showToast({ title: '反馈失败', icon: 'none' })
    })
  },

  trackEvent(eventName, properties = {}) {
    if (!this.data.openid) return
    api.post('/api/events', {
      openid: this.data.openid,
      event_name: eventName,
      properties,
    }).catch(() => {})
  }
})
