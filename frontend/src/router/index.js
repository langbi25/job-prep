import { createRouter, createWebHashHistory } from 'vue-router'
import MainLayout from '../layouts/MainLayout.vue'

// hash 模式：StaticFiles 托管无需 SPA fallback 配置
const routes = [
  {
    path: '/',
    component: MainLayout,
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', name: 'Dashboard', component: () => import('../views/Dashboard.vue'), meta: { title: '总览' } },
      { path: 'ingests', name: 'Ingests', component: () => import('../views/Ingests.vue'), meta: { title: '采集中心' } },
      { path: 'coverage', name: 'Coverage', component: () => import('../views/Coverage.vue'), meta: { title: '作战地图' } },
      { path: 'questions', name: 'Questions', component: () => import('../views/Questions.vue'), meta: { title: '题目池' } },
      { path: 'cards', name: 'Cards', component: () => import('../views/Cards.vue'), meta: { title: '知识库' } },
      { path: 'mock', name: 'Mock', component: () => import('../views/Mock.vue'), meta: { title: '模拟面试' } },
      { path: 'review', name: 'Review', component: () => import('../views/Review.vue'), meta: { title: '复习中心' } },
      { path: 'approvals', name: 'Approvals', component: () => import('../views/Approvals.vue'), meta: { title: '审批中心' } },
      { path: 'evolution', name: 'Evolution', component: () => import('../views/Evolution.vue'), meta: { title: '进化报告' } },
      { path: 'plans', name: 'Plans', component: () => import('../views/Plans.vue'), meta: { title: '改造方案' } },
    ],
  },
]

const router = createRouter({ history: createWebHashHistory(), routes })

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} · Job Prep` : 'Job Prep'
})

export default router
