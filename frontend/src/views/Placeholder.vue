<template>
  <div class="page">
    <el-alert v-if="error" :title="error" type="warning" :closable="false" style="margin-bottom: 16px" />
    <el-empty v-else-if="!loading && !name" :description="`「${$route.meta.title}」页面建设中`" />
    <template v-else>
      <h2>{{ name }}</h2>
      <pre>{{ JSON.stringify(data, null, 2) }}</pre>
    </template>
  </div>
</template>

<script setup>
import { ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'

const props = defineProps({ url: String })
const route = useRoute()
const name = ref('')
const data = ref(null)
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  name.value = route.meta.title
  if (!props.url) { loading.value = false; return }
  try { data.value = await api.get(props.url) } catch { error.value = '后端未连接，请先启动 API 服务 (端口 8765)' }
  loading.value = false
}

watch(() => route.path, load, { immediate: true })
</script>

<style scoped>
.page { padding: 24px; }
</style>
