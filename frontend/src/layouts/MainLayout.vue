<template>
  <el-container style="height: 100vh">
    <el-aside width="200px" class="aside">
      <div class="logo">🎯 Job Prep</div>
      <el-menu :default-active="$route.path" router background-color="#1d2530" text-color="#aeb7c2"
               active-text-color="#409eff"
               style="border-right: none; flex: 1; overflow-y: auto; overflow-x: hidden">
        <el-menu-item index="/console"><el-icon><ChatLineRound /></el-icon>Agent</el-menu-item>
        <el-menu-item index="/dashboard"><el-icon><Odometer /></el-icon>总览</el-menu-item>
        <el-menu-item index="/ingests"><el-icon><Upload /></el-icon>采集中心</el-menu-item>
        <el-menu-item index="/coverage"><el-icon><MapLocation /></el-icon>作战地图</el-menu-item>
        <el-menu-item index="/questions"><el-icon><Collection /></el-icon>题目池</el-menu-item>
        <el-menu-item index="/cards"><el-icon><Notebook /></el-icon>知识库</el-menu-item>
        <el-menu-item index="/ragboard"><el-icon><DataAnalysis /></el-icon>RAG 看板</el-menu-item>
        <el-menu-item index="/mcp"><el-icon><Connection /></el-icon>MCP 看板</el-menu-item>
        <el-menu-item index="/mock"><el-icon><ChatDotRound /></el-icon>模拟面试</el-menu-item>
        <el-menu-item index="/review"><el-icon><AlarmClock /></el-icon>复习中心</el-menu-item>
        <el-menu-item index="/approvals"><el-icon><Checked /></el-icon>审批中心</el-menu-item>
        <el-menu-item index="/plans"><el-icon><Tools /></el-icon>改造方案</el-menu-item>
        <el-menu-item index="/evolution"><el-icon><TrendCharts /></el-icon>进化报告</el-menu-item>
        <el-menu-item index="/retrospectives"><el-icon><Reading /></el-icon>复盘中心</el-menu-item>
        <el-menu-item index="/observability"><el-icon><Monitor /></el-icon>可观测中心</el-menu-item>
        <el-menu-item index="/traces"><el-icon><Share /></el-icon>调用树</el-menu-item>
        <el-menu-item index="/quality"><el-icon><Aim /></el-icon>回合质量</el-menu-item>
        <el-menu-item index="/agent-lab"><el-icon><Cpu /></el-icon>Agent 能力</el-menu-item>
        <el-menu-item index="/harness"><el-icon><Grid /></el-icon>Harness 管理</el-menu-item>
      </el-menu>
      <div class="backend-status">
        <el-tag :type="healthy ? 'success' : 'danger'" size="small" effect="dark">
          {{ healthy ? '后端在线' : '后端离线' }}
        </el-tag>
      </div>
    </el-aside>
    <el-main style="padding: 0; overflow: auto">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import api from '../api'

const healthy = ref(false)
let timer = null

async function check() {
  try {
    await api.get('/health', { timeout: 5000 })
    healthy.value = true
  } catch {
    healthy.value = false
  }
}

onMounted(() => {
  check()
  timer = setInterval(check, 15000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.aside { background: #1d2530; display: flex; flex-direction: column; }
.logo { color: #fff; font-size: 18px; font-weight: 700; padding: 20px 16px; letter-spacing: 1px; }
/* 菜单已有 15 项，默认 56px 行高在 1080p 下会被裁掉末尾几项；
   压到 42px 后全部可见，滚动条只在极端窄屏出现 */
.aside :deep(.el-menu-item) { height: 42px; line-height: 42px; }
.aside :deep(.el-menu) { scrollbar-width: thin; scrollbar-color: #3a4654 transparent; }
.aside :deep(.el-menu::-webkit-scrollbar) { width: 6px; }
.aside :deep(.el-menu::-webkit-scrollbar-thumb) { background: #3a4654; border-radius: 3px; }
.aside :deep(.el-menu::-webkit-scrollbar-track) { background: transparent; }
.backend-status { margin-top: auto; padding: 16px; }
</style>
