/**
 * 任务打卡看板组件
 * 支持连续点击5次触发管理员入口
 */

const api = require('../../utils/api')

Component({
  properties: {
    openid: {
      type: String,
      value: ''
    }
  },

  data: {
    tasks: [],
    completedCount: 0,
    totalCount: 0,
    tapCount: 0,
    tapTimer: null,
  },

  lifetimes: {
    attached() {
      this.loadTasks()
    }
  },

  methods: {
    async loadTasks() {
      if (!this.properties.openid) return
      try {
        const tasks = await api.get(`/api/tasks/today/${this.properties.openid}`)
        this.setData({
          tasks,
          completedCount: tasks.filter(t => t.is_completed).length,
          totalCount: tasks.length,
        })
      } catch (e) {
        console.error('[TaskBoard] 加载任务失败', e)
      }
    },

    async onTaskTap(e) {
      const { id, index } = e.currentTarget.dataset
      const task = this.data.tasks[index]
      if (task.is_completed) return

      try {
        await api.post('/api/tasks/checkin', { task_id: id, openid: this.properties.openid })
        this.setData({
          [`tasks[${index}].is_completed`]: true,
          completedCount: this.data.completedCount + 1,
        })
        wx.vibrateShort({ type: 'light' })
      } catch (e) {
        console.error('[TaskBoard] 打卡失败', e)
      }
    },

    /** 连续点击5次标题 → 触发管理员入口 */
    onHeaderTap() {
      this.data.tapCount++
      clearTimeout(this.data.tapTimer)

      if (this.data.tapCount >= 5) {
        this.data.tapCount = 0
        this.triggerEvent('adminTrigger')
        return
      }

      this.data.tapTimer = setTimeout(() => {
        this.data.tapCount = 0
      }, 800)
    },

    refresh() {
      this.loadTasks()
    }
  }
})
