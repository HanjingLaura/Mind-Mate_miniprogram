// Mind-Mate 小程序入口
App({
  onLaunch() {
    this.loginReady = this.login()
  },

  login() {
    return new Promise((resolve) => {
      wx.login({
        success: (res) => {
          if (res.code) {
            this.globalData.openid = 'user_' + res.code
            this.globalData.isDevMode = false
            console.log('[App] 登录成功', this.globalData.openid)
            resolve()
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
    apiBase: 'http://localhost:8000',
    isVip: false,
    isDevMode: false,
  }
})
