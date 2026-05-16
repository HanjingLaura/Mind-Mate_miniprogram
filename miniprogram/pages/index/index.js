/**
 * 心智同行 聊天主页
 * 核心功能：流式聊天 + 任务看板 + VIP 弹窗 + 历史入口
 */

const app = getApp()
const api = require('../../utils/api')
const util = require('../../utils/util')

Page({
  data: {
    openid: '',
    messages: [],
    inputText: '',
    isStreaming: false,
    streamingContent: '',
    scrollTarget: '',
    showVipModal: false,
    currentDate: '',
    isVip: false,
  },

  _requestTask: null,

  onLoad() {
    const openid = app.globalData.openid || 'dev_default'
    this.setData({
      openid,
      currentDate: util.getEffectiveDate(),
    })
    this.loadProfile()
  },

  onShow() {
    // 从设置页返回时刷新
    if (this.data.openid) {
      this.loadProfile()
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

  onInput(e) {
    this.setData({ inputText: e.detail.value })
  },

  onSend() {
    const text = this.data.inputText.trim()
    if (!text || this.data.isStreaming) return

    // 添加用户消息到列表
    const userMsg = {
      id: Date.now(),
      role: 'user',
      content: text,
    }
    this.setData({
      messages: [...this.data.messages, userMsg],
      inputText: '',
      isStreaming: true,
      streamingContent: '',
    })
    this.scrollToBottom()

    // 流式请求
    this._requestTask = api.streamChat(
      this.data.openid,
      text,
      // onChunk
      (chunk) => {
        this.setData({
          streamingContent: this.data.streamingContent + chunk,
        })
        this.scrollToBottom()
      },
      // onDone
      () => {
        const aiContent = this.data.streamingContent
        if (aiContent) {
          const aiMsg = {
            id: Date.now() + 1,
            role: 'assistant',
            content: aiContent,
          }
          this.setData({
            messages: [...this.data.messages, aiMsg],
            isStreaming: false,
            streamingContent: '',
          })
          // 刷新任务看板
          const taskBoard = this.selectComponent('#task-board')
          if (taskBoard) taskBoard.refresh()
        } else {
          this.setData({ isStreaming: false, streamingContent: '' })
        }
      },
      this.data.currentDate
    )
  },

  scrollToBottom() {
    setTimeout(() => {
      this.setData({ scrollTarget: 'scroll-bottom' })
    }, 50)
  },

  /** 历史按钮 — 跳转历史页 */
  onHistoryTap() {
    wx.navigateTo({ url: `/pages/history/history?openid=${this.data.openid}` })
  },

  /** 任务看板5连击 → 管理员入口 */
  onAdminTrigger() {
    wx.navigateTo({ url: `/pages/admin/admin?openid=${this.data.openid}` })
  },

  /** VIP 相关 */
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

      // 拉起微信支付
      wx.requestPayment({
        timeStamp: params.time_stamp,
        nonceStr: params.nonce_str,
        package: params.package,
        signType: params.sign_type,
        paySign: params.pay_sign,
        success: () => {
          wx.showToast({ title: 'VIP开通成功！', icon: 'success' })
          this.setData({ isVip: true })
          app.globalData.isVip = true
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

  onUnload() {
    if (this._requestTask) {
      this._requestTask.abort()
    }
  }
})
