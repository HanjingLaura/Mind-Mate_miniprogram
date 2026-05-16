/**
 * 网络请求封装
 */

const app = getApp()

/**
 * 通用请求方法
 */
function request(url, options = {}) {
  const { method = 'GET', data = {} } = options

  return new Promise((resolve, reject) => {
    wx.request({
      url: `${app.globalData.apiBase}${url}`,
      method,
      data,
      header: {
        'Content-Type': 'application/json',
      },
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data)
        } else {
          reject(res)
        }
      },
      fail(err) {
        console.error('[API] 请求失败:', url, err)
        reject(err)
      }
    })
  })
}

/**
 * 流式聊天请求 — 使用 wx.request 的 enableChunked 实现流式读取
 */
function streamChat(openid, content, onChunk, onDone, date) {
  const requestTask = wx.request({
    url: `${app.globalData.apiBase}/api/chat/send`,
    method: 'POST',
    data: { openid, content, date },
    enableChunked: true,
    success() {
      onDone && onDone()
    },
    fail(err) {
      console.error('[Stream] 请求失败', err)
      onDone && onDone()
    }
  })

  // 监听分块数据
  requestTask.onChunkReceived((res) => {
    try {
      const text = new TextDecoder('utf-8').decode(new Uint8Array(res.data))
      const lines = text.split('\n')

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const payload = line.slice(6).trim()
          if (payload === '[DONE]') {
            onDone && onDone()
            return
          }
          try {
            const parsed = JSON.parse(payload)
            if (parsed.content) {
              onChunk(parsed.content)
            }
          } catch (e) {
            // 忽略解析错误
          }
        }
      }
    } catch (e) {
      console.error('[Stream] 解析错误', e)
    }
  })

  return requestTask
}

module.exports = {
  request,
  streamChat,
  // 便捷方法
  get: (url) => request(url, { method: 'GET' }),
  post: (url, data) => request(url, { method: 'POST', data }),
  put: (url, data) => request(url, { method: 'PUT', data }),
}
