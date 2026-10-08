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
    { path: '/', name: 'dashboard', component: DashboardView, meta: { title: '控制台', eyebrow: '概览' } },
    { path: '/automations', name: 'automations', component: AutomationsView, meta: { title: '智能体', eyebrow: '智能体', agentWorkspace: true } },
    { path: '/run-agents', name: 'run-agents', component: RunAgentsView, meta: { title: '运行智能体', eyebrow: '运行' } },
    { path: '/new-automation', name: 'automation-new', component: StudioView, meta: { title: '创建智能体', eyebrow: '创建' } },
    { path: '/studio', redirect: '/new-automation' },
    { path: '/studio/new/:kind(flow|crew)', name: 'studio-new', component: StudioView, meta: { title: '创建智能体', eyebrow: '创建' } },
    { path: '/studio/:id', name: 'studio', component: StudioView, meta: { title: '编辑智能体', eyebrow: '编辑' } },
    { path: '/automations/:id/run', name: 'automation-run', component: AutomationRunView, meta: { title: '运行智能体', eyebrow: '运行', standalone: true } },
    { path: '/automations/:id/develop', name: 'automation-develop', component: ApiDevelopView, meta: { title: 'API 接入', eyebrow: '开发' } },
    { path: '/runs/:id?', name: 'runs', component: RunsView, meta: { title: '运行记录', eyebrow: '执行观察' } },
    { path: '/resources', name: 'resources', component: ResourcesView, meta: { title: '资源管理', eyebrow: '资源' } },
    { path: '/knowledge', name: 'knowledge', component: KnowledgeView, meta: { title: '知识库', eyebrow: '知识库' } },
    { path: '/knowledge/:id', name: 'knowledge-detail', component: KnowledgeView, meta: { title: '知识库详情', eyebrow: '知识库' } },
    { path: '/workspace', name: 'workspace', component: WorkspaceView, meta: { title: '用户与工作空间', eyebrow: '权限' } },
    { path: '/models', name: 'models', component: ModelsView, meta: { title: '模型连接', eyebrow: '设置' } },
    { path: '/model-default', name: 'model-default', component: DefaultModelView, meta: { title: '默认模型', eyebrow: '设置' } },
    { path: '/skill-dev/:id?', name: 'skill-dev', component: SkillDevView, meta: { title: 'Skill 开发环境', eyebrow: '开发' } },
  ],
})

router.beforeEach((to) => {
  if (to.meta.public) return true
  if (!localStorage.getItem('xuanshu_token')) return '/login'
  return true
})
export default router
