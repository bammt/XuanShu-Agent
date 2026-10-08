import { defineStore } from 'pinia'
import { api } from '../services/api'

export const usePlatformStore = defineStore('platform', {
  state: () => ({
    workflows: [], skills: [], plugins: [], knowledge: [], models: [], runs: [], workspaces: [], currentWorkspace: null,
    runtime: { connected_apps: { configured: false } },
    stats: { workflows: 0, published: 0, runs: 0, successful: 0 },
    loading: false, resourcesLoading: false, error: '', notice: '',
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
        const [overview, knowledge] = await Promise.all([api.overview(), api.knowledge()])
        Object.assign(this, overview); this.knowledge = knowledge; this.error = ''
        this.loadResources()
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
    async loadResources() {
      if (this._resourcesPromise) return this._resourcesPromise
      if (this.skills.length || this.plugins.length) return
      this.resourcesLoading = true
      this._resourcesPromise = (async () => { try {
        const [skills, plugins] = await Promise.all([api.skills(), api.plugins()])
        this.skills = skills; this.plugins = plugins
      } catch (error) {
        this.error = error.message
      } finally {
        this.resourcesLoading = false
        this._resourcesPromise = null
      } })()
      return this._resourcesPromise
    },
    notify(message) { this.notice = message; window.setTimeout(() => { this.notice = '' }, 2600) },
  },
})
