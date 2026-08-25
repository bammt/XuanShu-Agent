import { defineStore } from 'pinia'
import { api } from '../services/api'

export const usePlatformStore = defineStore('platform', {
  state: () => ({
    workflows: [], skills: [], plugins: [], knowledge: [], models: [], runs: [], workspaces: [], currentWorkspace: null,
    runtime: { connected_apps: { configured: false } },
    stats: { workflows: 0, published: 0, runs: 0, successful: 0 },
    loading: false, error: '', notice: '',
  }),
  getters: {
    canEdit: (state) => Boolean(state.currentWorkspace?.can_edit),
    defaultModel: (state) => state.models.find((item) => item.model_type !== 'embedding' && item.is_default) || null,
    defaultEmbeddingModel: (state) => state.models.find((item) => item.model_type === 'embedding' && item.is_default) || null,
    chatModels: (state) => state.models.filter((item) => item.model_type !== 'embedding'),
    embeddingModels: (state) => state.models.filter((item) => item.model_type === 'embedding'),
  },
  actions: {
    async load() {
      this.loading = true
      try {
        this.workspaces = await api.workspaces()
        const saved = Number(localStorage.getItem('xuanshu_workspace'))
        this.currentWorkspace = this.workspaces.find(x => x.id === saved) || this.workspaces[0] || null
        if (!this.currentWorkspace) throw new Error('当前账号还没有工作空间')
        localStorage.setItem('xuanshu_workspace', this.currentWorkspace.id)
        Object.assign(this, await api.overview()); this.knowledge = await api.knowledge(); this.error = ''
      }
      catch (error) { this.error = error.message }
      finally { this.loading = false }
    },
    async loadModels() {
      try {
        this.models = await api.models()
        this.error = ''
        return this.models
      } catch (error) {
        this.error = error.message
        return []
      }
    },
    notify(message) { this.notice = message; window.setTimeout(() => { this.notice = '' }, 2600) },
  },
})
