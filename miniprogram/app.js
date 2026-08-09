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
            this._loginWithCode(res.code).then(() => resolve(true)).catch(() => {
              if (this.globalData.env === 'cloud') {
                this._showLoginError()
                resolve(false)
              } else {
                this._fallbackLogin()
                resolve(true)
              }
            })
          } else {
            if (this.globalData.env === 'cloud') {
              this._showLoginError()
              resolve(false)
            } else {
              this._fallbackLogin()
              resolve(true)
            }
          }
        },
        fail: () => {
          if (this.globalData.env === 'cloud') {
            this._showLoginError()
            resolve(false)
          } else {
            this._fallbackLogin()
            resolve(true)
          }
        }
      })
    })
  },

  async _loginWithCode(code) {
    const api = require('./utils/api')
    const data = await api.post('/api/user/login', { code })
    this.globalData.openid = data.openid
    this.globalData.isVip = data.is_vip || false
    this.globalData.vipStatusReady = true
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

  _showLoginError() {
    this.globalData.openid = ''
    this.globalData.loginError = true
    wx.showModal({
      title: '登录失败',
      content: '暂时无法连接服务，请稍后重试。',
      showCancel: false,
    })
  },

  _randomId() {
    return Math.random().toString(36).substring(2, 10) + Date.now().toString(36)
  },

  globalData: {
    openid: '',
    isVip: false,
    vipStatusReady: false,
    isDevMode: false,
    loginError: false,
    env: 'local',
  }
})
