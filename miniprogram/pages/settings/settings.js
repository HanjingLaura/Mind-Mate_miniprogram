/**
 * 设置页 — 作息时间调整 + 通知 + VIP 状态
 */

const app = getApp()
const api = require('../../utils/api')
const config = require('../../utils/config')

Page({
  data: {
    openid: '',
    morningTime: '10:00',
    afternoonTime: '16:00',
    eveningTime: '21:00',
    isVip: false,
  },

  onLoad(options) {
    this.setData({ openid: options.openid || app.globalData.openid })
    this.loadSettings()
  },

  async loadSettings() {
    try {
      const profile = await api.get(`/api/user/profile/${this.data.openid}`)
      this.setData({
        morningTime: profile.morning_time || '10:00',
        afternoonTime: profile.afternoon_time || '16:00',
        eveningTime: profile.evening_time || '21:00',
        isVip: profile.is_vip,
      })
    } catch (e) {
      console.error('[Settings] 加载设置失败', e)
    }
  },

  async onMorningChange(e) {
    const time = e.detail.value
    this.setData({ morningTime: time })
    await this.saveSettings()
  },

  async onAfternoonChange(e) {
    if (!this.data.isVip) {
      wx.showToast({ title: 'VIP专属功能', icon: 'none' })
      return
    }
    const time = e.detail.value
    this.setData({ afternoonTime: time })
    await this.saveSettings()
  },

  async onEveningChange(e) {
    if (!this.data.isVip) {
      wx.showToast({ title: 'VIP专属功能', icon: 'none' })
      return
    }
    const time = e.detail.value
    this.setData({ eveningTime: time })
    await this.saveSettings()
  },

  async saveSettings() {
    try {
      await api.put(`/api/user/settings/${this.data.openid}`, {
        morning_time: this.data.morningTime,
        afternoon_time: this.data.afternoonTime,
        evening_time: this.data.eveningTime,
      })
      wx.showToast({ title: '已保存', icon: 'success' })
    } catch (e) {
      console.error('[Settings] 保存失败', e)
      wx.showToast({ title: '保存失败', icon: 'none' })
    }
  },

  onUpgradeTap() {
    if (this.data.isVip) return
    app.globalData.showVipOnShow = true
    wx.switchTab({ url: '/pages/index/index' })
  },

  onNotificationTap() {
    if (app.globalData.isDevMode) {
      wx.showToast({ title: '开发模式暂不支持', icon: 'none' })
      return
    }
    const templateId = config.SUBSCRIBE_TEMPLATE_ID
    if (!templateId) {
      wx.showToast({ title: '通知模板未配置', icon: 'none' })
      return
    }
    wx.requestSubscribeMessage({
      tmplIds: [templateId],
      success: (res) => {
        if (res[templateId] === 'accept') {
          api.post('/api/user/subscribe_auth', {
            openid: this.data.openid,
            template_id: templateId,
          })
          wx.showToast({ title: '已开启提醒', icon: 'success' })
        }
      },
      fail: () => {
        wx.showToast({ title: '授权失败', icon: 'none' })
      }
    })
  },

  onAdminTap() {
    wx.navigateTo({ url: `/pages/admin/admin?openid=${this.data.openid}` })
  }
})
