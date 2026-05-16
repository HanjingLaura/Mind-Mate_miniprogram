// Mind-Mate 小程序入口
App({
  onLaunch() {
    // 静默登录
    this.login()
  },

  login() {
    wx.login({
      success: (res) => {
        if (res.code) {
          // 将 code 发送到后端换取 openid
          // MVP 阶段：前端模拟 openid，生产环境需后端通过 code2session 获取
          this.globalData.openid = 'user_' + res.code
          console.log('[App] 登录成功', this.globalData.openid)
        }
      },
      fail: (err) => {
        console.error('[App] 登录失败', err)
        // 降级处理：使用本地生成的临时 ID
        this.globalData.openid = 'dev_' + Date.now()
      }
    })
  },

  globalData: {
    openid: '',
    apiBase: 'http://localhost:8000', // 开发环境地址，生产替换
    isVip: false,
  }
})
