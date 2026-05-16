/**
 * 管理员/种子用户 内测激活页
 * 通过内测暗号直接调用后端 bypass_upgrade 接口
 */

const api = require('../../utils/api')

Page({
  data: {
    openid: '',
    secretKey: '',
    activating: false,
    resultText: '',
    resultSuccess: false,
  },

  onLoad(options) {
    this.setData({ openid: options.openid || '' })
  },

  onKeyInput(e) {
    this.setData({
      secretKey: e.detail.value,
      resultText: '',
    })
  },

  async onActivate() {
    if (!this.data.secretKey.trim() || this.data.activating) return

    this.setData({ activating: true, resultText: '' })

    try {
      // 先获取用户 ID
      const profile = await api.get(`/api/user/profile/${this.data.openid}`)
      if (!profile || !profile.id) {
        this.setData({
          resultText: '用户不存在',
          resultSuccess: false,
          activating: false,
        })
        return
      }

      const res = await api.post('/api/admin/bypass_upgrade', {
        user_id: profile.id,
        secret_key: this.data.secretKey,
      })

      this.setData({
        resultText: res.message || 'VIP 激活成功！',
        resultSuccess: true,
        activating: false,
      })

      wx.vibrateShort({ type: 'heavy' })

    } catch (e) {
      const msg = e?.data?.detail || '激活失败，暗号有误'
      this.setData({
        resultText: msg,
        resultSuccess: false,
        activating: false,
      })
    }
  }
})
