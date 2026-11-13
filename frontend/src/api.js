import axios from 'axios'
import { ElMessage, ElMessageBox } from 'element-plus'

const api = axios.create({ baseURL: '', timeout: 120000 })

// 访问令牌（APP_TOKEN 开启鉴权时必带）。存 localStorage：刷新页面不丢，且
// 这是唯一合理位置——没有后端会话，token 本身就是身份。
const TOKEN_KEY = 'app_token'
export function getToken() { return localStorage.getItem(TOKEN_KEY) || '' }
export function setToken(t) { localStorage.setItem(TOKEN_KEY, (t || '').trim()) }

api.interceptors.request.use((cfg) => {
  const t = getToken()
  if (t) cfg.headers['X-API-Token'] = t
  return cfg
})

// 401：token 缺失/失效。弹框收集后重试原请求一次。
// 用标志位防并发请求同时触发多个弹框：第一个 401 收集期间，后续 401 直接排队等新 token。
let prompting = null
api.interceptors.response.use(
  (resp) => resp.data,
  async (err) => {
    const detail = err.response?.data?.detail
    const msg = typeof detail === 'string' ? detail : (detail?.map?.((d) => d.msg).join('; ') || err.message)
    if (err.response?.status === 401) {
      try {
        prompting = prompting || ElMessageBox.prompt('本服务开启了访问鉴权，请输入访问令牌（APP_TOKEN）', '需要访问令牌', {
          inputType: 'password', confirmButtonText: '保存并重试', cancelButtonText: '取消',
        })
        const { value } = await prompting
        prompting = null
        if (value && value.trim()) {
          setToken(value)
          return api.request(err.config)   // 换新 token 重放原请求
        }
      } catch {
        prompting = null   // 用户取消
      }
      return Promise.reject(err)
    }
    ElMessage.error(`请求失败: ${msg}`)
    return Promise.reject(err)
  }
)

export default api
