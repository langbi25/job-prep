import axios from 'axios'
import { ElMessage } from 'element-plus'

const api = axios.create({ baseURL: '', timeout: 120000 })

api.interceptors.response.use(
  (resp) => resp.data,
  (err) => {
    const detail = err.response?.data?.detail
    const msg = typeof detail === 'string' ? detail : (detail?.map?.((d) => d.msg).join('; ') || err.message)
    ElMessage.error(`请求失败: ${msg}`)
    return Promise.reject(err)
  }
)

export default api
