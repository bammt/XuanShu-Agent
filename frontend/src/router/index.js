import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from '../views/DashboardView.vue'
import AutomationsView from '../views/AutomationsView.vue'
import RunAgentsView from '../views/RunAgentsView.vue'
import StudioView from '../views/StudioView.vue'
import RunsView from '../views/RunsView.vue'
import ResourcesView from '../views/ResourcesView.vue'
import ModelsView from '../views/ModelsView.vue'
import DefaultModelView from '../views/DefaultModelView.vue'
import SkillDevView from '../views/SkillWorkspaceView.vue'
import AutomationRunView from '../views/AutomationRunView.vue'
import LoginView from '../views/LoginView.vue'
import PublicRunView from '../views/PublicRunView.vue'
import WorkspaceView from '../views/WorkspaceView.vue'
import ApiDevelopView from '../views/ApiDevelopView.vue'
import KnowledgeView from '../views/KnowledgeView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: LoginView, meta: { standalone: true, public: true } },
    { path: '/public/:token', name: 'public-run', component: PublicRunView, meta: { standalone: true, public: true } },
    { path: '/', name: 'dashboard', component: DashboardView, meta: { title: '控制台', eyebrow: 'OVERVIEW' } },
    { path: '/automations', name: 'automations', component: AutomationsView, meta: { title: '智能体', eyebrow: 'APPLICATIONS' } },
    { path: '/run-agents', name: 'run-agents', component: RunAgentsView, meta: { title: '运行智能体', eyebrow: 'RUNTIME' } },
    { path: '/new-automation', name: 'automation-new', component: StudioView, meta: { title: '玄枢编排台', eyebrow: 'BUILDER' } },
    { path: '/studio', redirect: '/new-automation' },
    { path: '/studio/new/:kind(flow|crew)', name: 'studio-new', component: StudioView, meta: { title: '玄枢编排台', eyebrow: 'BUILDER' } },
    { path: '/studio/:id', name: 'studio', component: StudioView, meta: { title: '玄枢编排台', eyebrow: 'BUILDER' } },
    { path: '/automations/:id/run', name: 'automation-run', component: AutomationRunView, meta: { title: '运行智能体', eyebrow: 'APP', standalone: true } },
    { path: '/automations/:id/develop', name: 'automation-develop', component: ApiDevelopView, meta: { title: 'API 接入', eyebrow: 'DEVELOP' } },
    { path: '/runs/:id?', name: 'runs', component: RunsView, meta: { title: 'Runs & Traces', eyebrow: 'OBSERVABILITY' } },
    { path: '/resources', name: 'resources', component: ResourcesView, meta: { title: 'Agent Capabilities', eyebrow: 'ASSETS' } },
    { path: '/knowledge', name: 'knowledge', component: KnowledgeView, meta: { title: '知识库', eyebrow: 'KNOWLEDGE' } },
    { path: '/knowledge/:id', name: 'knowledge-detail', component: KnowledgeView, meta: { title: '知识库详情', eyebrow: 'KNOWLEDGE' } },
    { path: '/workspace', name: 'workspace', component: WorkspaceView, meta: { title: '用户与工作空间', eyebrow: 'ACCESS' } },
    { path: '/models', name: 'models', component: ModelsView, meta: { title: '模型连接', eyebrow: 'SETTINGS' } },
    { path: '/model-default', name: 'model-default', component: DefaultModelView, meta: { title: '默认模型', eyebrow: 'SETTINGS' } },
    { path: '/skill-dev/:id?', name: 'skill-dev', component: SkillDevView, meta: { title: 'Skill 开发环境', eyebrow: 'DEVELOPER' } },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  if (!localStorage.getItem('xuanshu_token')) return '/login'
  return true
})
export default router
