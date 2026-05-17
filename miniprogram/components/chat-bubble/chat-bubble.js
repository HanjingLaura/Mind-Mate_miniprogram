/**
 * 聊天气泡组件
 */
Component({
  properties: {
    role: {
      type: String,
      value: 'user'
    },
    content: {
      type: String,
      value: ''
    },
    time: {
      type: String,
      value: ''
    },
    isTyping: {
      type: Boolean,
      value: false
    }
  }
})
