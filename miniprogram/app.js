// Mind-Mate 小程序入口
const { USE_CLOUD, CLOUD_ENV_ID } = require('./utils/config')

App({
  onLaunch() {
    // 检测云托管环境
    if (USE_CLOUD && wx.cloud && CLOUD_ENV_ID) {
      try {
        wx.cloud.init({ env: CLOUD_ENV_ID })
        this.globalData.env = 'cloud'
      } catch (e) {
        this.globalData.env = 'local'
      }
    } else {
      this.globalData.env = 'local'
    }
    this.loginReady = this.login()
  },

  login() {
    return new Promise((resolve) => {
      wx.login({
        success: (res) => {
          if (res.code) {
            this._loginWithCode(res.code).then(resolve).catch(() => {
              this._fallbackLogin()
              resolve()
            })
          } else {
            this._fallbackLogin()
            resolve()
          }
        },
        fail: () => {
          this._fallbackLogin()
          resolve()
        }
      })
    })
  },

  async _loginWithCode(code) {
    const api = require('./utils/api')
    const data = await api.post('/api/user/login', { code })
    this.globalData.openid = data.openid
    this.globalData.isVip = data.is_vip || false
    this.globalData.isDevMode = false
    console.log('[App] 登录成功', data.openid)
  },

  _fallbackLogin() {
    let openid = wx.getStorageSync('fallback_openid')
    if (openid) {
      this.globalData.openid = openid
    } else {
      openid = 'dev_' + this._randomId()
      this.globalData.openid = openid
      wx.setStorageSync('fallback_openid', openid)
    }
    this.globalData.isDevMode = true
    console.log('[App] 开发模式', openid)
  },

  _randomId() {
    return Math.random().toString(36).substring(2, 10) + Date.now().toString(36)
  },

  globalData: {
    openid: '',
    isVip: false,
    isDevMode: false,
    env: 'local',
  }
})
