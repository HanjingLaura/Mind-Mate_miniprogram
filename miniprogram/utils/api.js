/**
 * 网络请求封装
 * 支持 wx.request（本地开发）和 wx.cloud.callContainer（云托管）
 */

const app = getApp()
const { CLOUD_ENV_ID, CLOUD_SERVICE_NAME } = require('./config')

const LOCAL_API_BASE = 'http://localhost:8000'

/**
 * 通用请求方法 — 自动选择传输通道
 */
function request(url, options = {}) {
  const { method = 'GET', data = {} } = options

  if (app.globalData.env === 'cloud') {
    return _cloudRequest(url, method, data)
  }
  return _localRequest(url, method, data)
}

function _localRequest(url, method, data) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${LOCAL_API_BASE}${url}`,
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

function _cloudRequest(url, method, data) {
  // 1. 必须先声明并初始化变量
  let targetPath = url.startsWith('/') ? url : '/' + url

  // 2. 然后才能在 console.log 中安全地使用它
  console.log('[API] 云托管请求:', { 
    env: CLOUD_ENV_ID, 
    service: CLOUD_SERVICE_NAME, 
    path: targetPath 
  })
  
  return new Promise((resolve, reject) => {
    wx.cloud.callContainer({
      config: {
        env: CLOUD_ENV_ID,
      },
      path: targetPath, // 传递规整后的路径
      method,
      data,
      header: {
        'X-WX-SERVICE': CLOUD_SERVICE_NAME,
        'content-type': 'application/json',
      },
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          resolve(res.data)
        } else {
          reject(res)
        }
      },
      fail(err) {
        console.error('[API] 云托管请求失败:', targetPath, err)
        reject(err)
      }
    })
  })
}

/**
 * 流式聊天请求 — 本地用 SSE，云端用同步接口
 */
function streamChat(openid, content, onChunk, onDone, onTaskChanges) {
  if (app.globalData.env === 'cloud') {
    return _cloudStreamChat(openid, content, onChunk, onDone, onTaskChanges)
  }
  return _localStreamChat(openid, content, onChunk, onDone, onTaskChanges)
}

function _localStreamChat(openid, content, onChunk, onDone, onTaskChanges) {
  let doneCalled = false

  function safeDone() {
    if (doneCalled) return
    doneCalled = true
    onDone && onDone()
  }

  const requestTask = wx.request({
    url: `${LOCAL_API_BASE}/api/chat/send`,
    method: 'POST',
    data: { openid, content },
    enableChunked: true,
    timeout: 120000,
    success() {
      safeDone()
    },
    fail(err) {
      console.error('[Stream] 请求失败', err)
      safeDone()
    }
  })

  requestTask.onChunkReceived((res) => {
    try {
      const text = new TextDecoder('utf-8').decode(new Uint8Array(res.data))
      const lines = text.split('\n')

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const payload = line.slice(6).trim()
          if (payload === '[DONE]') {
            safeDone()
            return
          }
          try {
            const parsed = JSON.parse(payload)
            if (parsed.task_changes) {
              onTaskChanges && onTaskChanges(parsed.task_changes)
            }
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

async function _cloudStreamChat(openid, content, onChunk, onDone, onTaskChanges) {
  try {
    const result = await _cloudRequest('/api/chat/send', 'POST', {
      openid, content, sync: true
    })
    if (result.task_changes) {
      onTaskChanges && onTaskChanges(result.task_changes)
    }
    if (result.content) {
      onChunk && onChunk(result.content)
    }
  } catch (e) {
    console.error('[CloudStream] 失败', e)
  }
  onDone && onDone()
  return null
}

module.exports = {
  request,
  streamChat,
  get: (url) => request(url, { method: 'GET' }),
  post: (url, data) => request(url, { method: 'POST', data }),
  put: (url, data) => request(url, { method: 'PUT', data }),
  del: (url, data) => request(url, { method: 'DELETE', data }),
}
