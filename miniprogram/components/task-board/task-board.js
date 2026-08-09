/**
 * 任务打卡看板组件 — 始终可见，支持添加/删除/打卡
 * 连续点击5次标题触发管理员入口
 */

const api = require('../../utils/api')

Component({
  properties: {
    openid: {
      type: String,
      value: '',
      observer(openid) {
        if (openid) this.loadTasks()
      }
    }
  },

  data: {
    tasks: [],
    completedCount: 0,
    totalCount: 0,
    progressPercent: 0,
    expanded: true,
    tapCount: 0,
    tapTimer: null,
    newTask: '',
    executionMemory: null,
    hasMemoryData: false,
    memoryExpanded: false,
    showEditDialog: false,
    editingTaskId: 0,
    editingTaskIndex: -1,
    editingTaskContent: '',
  },

  _highlightTimer: null,

  lifetimes: {
    attached() {
      this.loadTasks()
    }
  },

  methods: {
    _updateCounts(tasks) {
      const completedCount = tasks.filter(t => t.is_completed).length
      const totalCount = tasks.length
      const progressPercent = totalCount > 0 ? Math.round(completedCount / totalCount * 100) : 0
      this.setData({ completedCount, totalCount, progressPercent })
    },

    async loadTasks() {
      if (!this.properties.openid) return
      try {
        const [tasks, executionMemory] = await Promise.all([
          api.get(`/api/tasks/today/${this.properties.openid}`),
          api.get(`/api/tasks/memory/${this.properties.openid}`).catch(() => null),
        ])
        const hasMemoryData = !!(executionMemory && (
          executionMemory.yesterday.total_count > 0 ||
          executionMemory.recent.active_days > 0
        ))
        this.setData({ tasks, executionMemory, hasMemoryData })
        this._updateCounts(tasks)
      } catch (e) {
        console.error('[TaskBoard] 加载任务失败', e)
      }
    },

    onToggleExpand() {
      this.setData({ expanded: !this.data.expanded })
    },

    onToggleMemory() {
      const nextExpanded = !this.data.memoryExpanded
      this.setData({ memoryExpanded: nextExpanded })
      this._trackEvent('execution_memory_toggle', {
        expanded: nextExpanded,
      })
    },

    async onTaskTap(e) {
      const { id, index } = e.currentTarget.dataset
      const task = this.data.tasks[index]
      try {
        const res = await api.post('/api/tasks/checkin', { task_id: id, openid: this.properties.openid })
        this._trackEvent(res.is_completed ? 'task_checkin' : 'task_restore', { task_id: id })
        this.setData({ [`tasks[${index}].is_completed`]: res.is_completed })
        this._updateCounts(this.data.tasks)
        wx.vibrateShort({ type: 'light' })
      } catch (e) {
        console.error('[TaskBoard] 打卡失败', e)
      }
    },

    onEditTask(e) {
      const { id, index } = e.currentTarget.dataset
      const task = this.data.tasks[index]
      if (!task) return
      this.setData({
        showEditDialog: true,
        editingTaskId: id,
        editingTaskIndex: index,
        editingTaskContent: task.content,
      })
    },

    onEditInput(e) {
      this.setData({ editingTaskContent: e.detail.value })
    },

    onCancelEdit() {
      this.setData({ showEditDialog: false, editingTaskIndex: -1 })
    },

    async onConfirmEdit() {
      const content = this.data.editingTaskContent.trim()
      const index = this.data.editingTaskIndex
      if (!content || index < 0) return

      try {
        const task = await api.post('/api/tasks/edit', {
          task_id: this.data.editingTaskId,
          openid: this.properties.openid,
          content,
        })
        this.setData({
          [`tasks[${index}]`]: task,
          showEditDialog: false,
          editingTaskIndex: -1,
        })
        this._trackEvent('task_edit', { task_id: task.id, source: 'manual' })
      } catch (e) {
        console.error('[TaskBoard] 编辑失败', e)
        wx.showToast({ title: '保存失败', icon: 'none' })
      }
    },

    onDelete(e) {
      const { id, index } = e.currentTarget.dataset
      wx.showModal({
        title: '删除任务',
        content: '确定删除这个任务？',
        success: async (res) => {
          if (res.confirm) {
            try {
              await api.del('/api/tasks/delete', { task_id: id, openid: this.properties.openid })
              this._trackEvent('task_delete', { task_id: id })
              const tasks = this.data.tasks.filter((_, i) => i !== index)
              this.setData({ tasks })
              this._updateCounts(tasks)
            } catch (e) {
              console.error('[TaskBoard] 删除失败', e)
              wx.showToast({ title: '删除失败', icon: 'none' })
            }
          }
        }
      })
    },

    onNewTaskInput(e) {
      this.setData({ newTask: e.detail.value })
    },

    async onAddTask() {
      const content = this.data.newTask.trim()
      if (!content) return

      try {
        const task = await api.post('/api/tasks/add', {
          openid: this.properties.openid,
          content,
        })
        this._trackEvent('task_created', { task_id: task.id, source: 'manual' })
        const tasks = [...this.data.tasks, task]
        this.setData({ tasks, newTask: '' })
        this._updateCounts(tasks)
      } catch (e) {
        console.error('[TaskBoard] 添加失败', e)
        wx.showToast({ title: '添加失败', icon: 'none' })
      }
    },

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
    },

    async refreshWithChanges(changes) {
      if (!changes || changes.length === 0) {
        this.loadTasks()
        return
      }

      changes.forEach(c => {
        const eventName = c.action === 'done'
          ? 'task_checkin'
          : (c.action === 'add' ? 'task_created' : `task_${c.action}`)
        this._trackEvent(eventName, {
          task_id: c.task_id || 0,
          source: 'ai',
          action: c.action,
        })
      })

      // 分类变更：删除 vs 其他
      const changeMap = {}
      const deleteIds = new Set()
      for (const c of changes) {
        if (c.action === 'delete' && c.task_id) {
          deleteIds.add(c.task_id)
        } else if (c.task_id) {
          changeMap[c.task_id] = c.action
        }
      }

      // 删除任务先播放淡出动画
      if (deleteIds.size > 0) {
        const withDelete = this.data.tasks.map(t => ({
          ...t,
          highlight: deleteIds.has(t.id) ? 'delete' : (changeMap[t.id] || '')
        }))
        this.setData({ tasks: withDelete })

        // 等待淡出动画完成
        await new Promise(r => setTimeout(r, 600))
      }

      // 从服务器刷新最新状态
      await this.loadTasks()

      // 对非删除操作应用高亮
      if (Object.keys(changeMap).length > 0) {
        const highlighted = this.data.tasks.map(t => ({
          ...t,
          highlight: changeMap[t.id] || ''
        }))
        this.setData({ tasks: highlighted })

        // 新增任务时震动反馈
        if (changes.some(c => c.action === 'add')) {
          wx.vibrateShort({ type: 'medium' })
        }

        // 2秒后清除高亮
        clearTimeout(this._highlightTimer)
        this._highlightTimer = setTimeout(() => {
          const cleared = this.data.tasks.map(t => ({ ...t, highlight: '' }))
          this.setData({ tasks: cleared })
        }, 2000)
      }
    },

    _trackEvent(eventName, properties = {}) {
      if (!this.properties.openid) return
      api.post('/api/events', {
        openid: this.properties.openid,
        event_name: eventName,
        properties,
      }).catch(() => {})
    }
  }
})
