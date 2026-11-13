<template>
  <div class="qa-container">
    <!-- 左侧：聊天主区域 -->
    <div class="chat-main">
      <div class="chat-history" ref="chatHistoryRef">
        <div v-if="messages.length === 0" class="welcome-box">
          <div class="welcome-icon">🔬</div>
          <h3>NetResearch AI 科研助手</h3>
          <p>已就绪。您可以询问文献中的任何细节，我会提供真实的原文出处。</p>
        </div>

        <div v-for="(msg, index) in messages" :key="index" class="message-row" :class="msg.role">
          <div class="avatar" :class="msg.role">
            {{ msg.role === 'user' ? '我' : msg.role === 'imported' ? 'GPT' : 'AI' }}
          </div>
          <div class="bubble">
            <div v-if="msg.role === 'ai'" class="markdown-body" v-html="renderMarkdown(msg.content)"></div>
            <div v-else class="user-text">{{ msg.content }}</div>
          </div>
        </div>

        <div v-if="loading" class="message-row ai">
          <div class="avatar ai">AI</div>
          <div class="bubble loading">
            <el-icon class="is-loading"><Loading /></el-icon> 正在检索文献并生成回答...
          </div>
        </div>
      </div>

      <div class="input-box">
        <!-- 修复：value 改为空字符串 -->
        <div class="scope-selector">
          <span class="scope-label">检索范围：</span>
          <el-select v-model="selectedPaperId" placeholder="全库联合检索（默认）" clearable size="small" style="width: 250px">
            <el-option label="全库联合检索（所有论文）" value="" />
            <el-option 
              v-for="p in paperList" 
              :key="p.id" 
              :label="p.title" 
              :value="p.id" 
            />
          </el-select>
        </div>

        <el-input
          v-model="query"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 4 }"
          placeholder="输入您的问题（按 Enter 发送，Shift + Enter 换行）"
          @keydown.enter.prevent="handleEnter"
          class="chat-input"
        />
        <div class="input-actions">
          <span class="hint">基于 LangGraph 多步推理与 DeepSeek 大模型，支持多论文联合问答</span>
          <div class="action-buttons">
            <el-button :disabled="loading" @click="importDialogVisible = true">
              导入 ChatGPT 对话
            </el-button>
            <el-button type="primary" :loading="loading" @click="sendMessage" round>
              发送
            </el-button>
          </div>
        </div>
      </div>

      <el-dialog v-model="importDialogVisible" title="导入 ChatGPT 对话" width="640px">
        <p class="import-hint">粘贴从 ChatGPT 网页复制的对话内容。记录只保存在当前浏览器，不会上传到后端。</p>
        <el-input
          v-model="importedText"
          type="textarea"
          :autosize="{ minRows: 8, maxRows: 18 }"
          placeholder="在此粘贴 ChatGPT 对话..."
        />
        <template #footer>
          <el-button @click="importDialogVisible = false">取消</el-button>
          <el-button type="primary" @click="importConversation">导入</el-button>
        </template>
      </el-dialog>
    </div>

    <!-- 右侧：Agent 思考与证据溯源面板 -->
    <div class="side-panel">
      <div class="panel-header">
        <h3>Agent 执行轨迹</h3>
      </div>
      
      <div class="panel-content">
        <el-empty v-if="!currentSteps.length && !currentContexts.length" description="发送消息后，此处将展示执行过程与原文证据" :image-size="80" />
        
        <template v-else>
          <!-- 思考步骤 -->
          <div v-if="currentSteps.length" class="panel-section">
            <h4 class="section-title"><el-icon><Guide /></el-icon> 执行步骤</h4>
            <el-timeline>
              <el-timeline-item
                v-for="(step, idx) in currentSteps"
                :key="idx"
                :timestamp="'步骤 ' + (idx + 1)"
                placement="top"
                type="primary"
              >
                {{ step }}
              </el-timeline-item>
            </el-timeline>
          </div>

          <!-- 证据溯源 -->
          <div v-if="currentContexts.length" class="panel-section">
            <h4 class="section-title"><el-icon><Document /></el-icon> 溯源证据 ({{ currentContexts.length }})</h4>
            <el-collapse v-model="activeEvidence">
              <el-collapse-item
                v-for="(ctx, idx) in currentContexts"
                :key="idx"
                :name="idx.toString()"
              >
                <template #title>
                  <div class="evidence-header">
                    <el-tag size="small" type="success" effect="dark">[{{ idx + 1 }}]</el-tag>
                    <span class="paper-title">{{ ctx.paper_title }}</span>
                    <span class="page-num">第 {{ ctx.page_number }} 页</span>
                  </div>
                </template>
                <div class="evidence-text">{{ ctx.text }}</div>
              </el-collapse-item>
            </el-collapse>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Loading, Guide, Document } from '@element-plus/icons-vue'
import { marked } from 'marked'
import { chatWithAgent, fetchPapers } from '@/api/papers'

interface Message {
  role: 'user' | 'ai' | 'imported'
  content: string
}

const MESSAGE_STORAGE_KEY = 'netresearch-ai-qa-messages'

const isMessage = (value: unknown): value is Message => {
  if (!value || typeof value !== 'object') return false
  const message = value as Record<string, unknown>
  return ['user', 'ai', 'imported'].includes(String(message.role))
    && typeof message.content === 'string'
}

const loadStoredMessages = (): Message[] => {
  try {
    const stored = localStorage.getItem(MESSAGE_STORAGE_KEY)
    if (!stored) return []

    const parsed: unknown = JSON.parse(stored)
    if (!Array.isArray(parsed) || !parsed.every(isMessage)) {
      throw new Error('本地聊天记录格式无效')
    }
    return parsed
  } catch (error) {
    console.error('读取本地聊天记录失败', error)
    ElMessage.error('读取本地聊天记录失败，请检查浏览器存储。')
    return []
  }
}

const persistMessages = (value: Message[]): boolean => {
  try {
    localStorage.setItem(MESSAGE_STORAGE_KEY, JSON.stringify(value))
    return true
  } catch (error) {
    console.error('保存本地聊天记录失败', error)
    ElMessage.error('保存聊天记录失败，请检查浏览器存储空间。')
    return false
  }
}

const query = ref('')
const loading = ref(false)
const messages = ref<Message[]>(loadStoredMessages())
const chatHistoryRef = ref<HTMLElement | null>(null)
const importDialogVisible = ref(false)
const importedText = ref('')

// 下拉框：论文列表与选中状态（类型加上 string）
const paperList = ref<any[]>([])
const selectedPaperId = ref<number | string>("")

// 右侧面板数据
const currentSteps = ref<string[]>([])
const currentContexts = ref<any[]>([])
const activeEvidence = ref<string[]>(['0'])

// 页面加载时获取论文列表填充下拉框
onMounted(async () => {
  try {
    const res: any = await fetchPapers()
    if (Array.isArray(res)) {
      paperList.value = res
    } else if (res && res.data) {
      paperList.value = res.data
    }
  } catch (e) {
    console.error("获取论文列表失败", e)
  }
})

// Markdown 渲染
const renderMarkdown = (text: string) => {
  if (!text) return ''
  return marked.parse(text)
}

const handleEnter = (e: KeyboardEvent) => {
  if (!e.shiftKey) {
    sendMessage()
  }
}

const scrollToBottom = async () => {
  await nextTick()
  if (chatHistoryRef.value) {
    chatHistoryRef.value.scrollTop = chatHistoryRef.value.scrollHeight
  }
}

const importConversation = () => {
  const content = importedText.value.trim()
  if (!content) {
    ElMessage.warning('请先粘贴 ChatGPT 对话内容。')
    return
  }

  messages.value.push({ role: 'imported', content })
  const saved = persistMessages(messages.value)
  importedText.value = ''
  importDialogVisible.value = false
  if (saved) ElMessage.success('ChatGPT 对话已导入并保存在当前浏览器。')
  scrollToBottom()
}

const sendMessage = async () => {
  const q = query.value.trim()
  if (!q || loading.value) return

  messages.value.push({ role: 'user', content: q })
  persistMessages(messages.value)
  query.value = ''
  loading.value = true
  scrollToBottom()

  try {
    // 如果 selectedPaperId 是空字符串，就传 undefined
    const paperIdToSend = selectedPaperId.value === "" ? undefined : Number(selectedPaperId.value)
    const res: any = await chatWithAgent(q, paperIdToSend)
    
    const data = res.answer ? res : (res.data || res)

    messages.value.push({ role: 'ai', content: data.answer || '未获取到回答' })
    persistMessages(messages.value)
    
    // 更新右侧面板
    currentSteps.value = data.steps || []
    currentContexts.value = data.contexts || []
    
  } catch (error) {
    ElMessage.error('请求失败，请检查网络或后端日志')
    messages.value.push({ role: 'ai', content: '网络异常，生成失败。' })
    persistMessages(messages.value)
  } finally {
    loading.value = false
    scrollToBottom()
  }
}
</script>

<style scoped>
/* 保持你原有的样式即可 */
.qa-container { display: flex; height: calc(100vh - 120px); gap: 20px; padding: 0 4px; }
.chat-main { flex: 1; display: flex; flex-direction: column; background: #fff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.04); border: 1px solid #ebeef5; overflow: hidden; }
.chat-history { flex: 1; padding: 24px; overflow-y: auto; background: #fafafa; }
.welcome-box { text-align: center; margin-top: 80px; color: #606266; }
.welcome-icon { font-size: 40px; margin-bottom: 16px; }
.welcome-box h3 { color: #303133; margin-bottom: 8px; }
.message-row { display: flex; margin-bottom: 24px; gap: 12px; }
.message-row.user { flex-direction: row-reverse; }
.avatar { width: 36px; height: 36px; border-radius: 10px; display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: bold; flex-shrink: 0; }
.avatar.ai { background: #ecf5ff; color: #409eff; }
.avatar.user { background: #303133; color: #fff; }
.avatar.imported { background: #f0f9eb; color: #67c23a; }
.bubble { max-width: 80%; padding: 12px 16px; border-radius: 12px; font-size: 14px; line-height: 1.6; }
.message-row.ai .bubble { background: #fff; border: 1px solid #ebeef5; border-top-left-radius: 2px; }
.message-row.user .bubble { background: #409eff; color: #fff; border-top-right-radius: 2px; }
.message-row.imported .bubble { background: #fff; border: 1px solid #d9ecff; border-top-left-radius: 2px; }
.user-text { white-space: pre-wrap; overflow-wrap: anywhere; }
.bubble.loading { color: #909399; display: flex; align-items: center; gap: 8px; }
:deep(.markdown-body p) { margin: 0 0 10px; }
:deep(.markdown-body h1), :deep(.markdown-body h2), :deep(.markdown-body h3) { margin: 16px 0 8px; font-size: 16px; }
:deep(.markdown-body ul) { padding-left: 20px; margin-bottom: 10px; }
:deep(.markdown-body li) { margin-bottom: 4px; }
:deep(.markdown-body strong) { color: #303133; }
.input-box { padding: 16px 24px; background: #fff; border-top: 1px solid #ebeef5; }
.scope-selector { display: flex; align-items: center; margin-bottom: 12px; font-size: 13px; color: #606266; }
.scope-label { font-weight: bold; margin-right: 8px; }
.chat-input :deep(.el-textarea__inner) { box-shadow: none; padding: 12px; background: #f5f7fa; border-radius: 8px; font-size: 14px; }
.chat-input :deep(.el-textarea__inner:focus) { background: #fff; box-shadow: 0 0 0 1px #409eff inset; }
.input-actions { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-top: 12px; }
.action-buttons { display: flex; gap: 8px; }
.hint { font-size: 12px; color: #909399; }
.import-hint { margin: 0 0 12px; color: #606266; font-size: 13px; }
.side-panel { width: 380px; background: #fff; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,0.04); border: 1px solid #ebeef5; display: flex; flex-direction: column; }
.panel-header { padding: 16px 20px; border-bottom: 1px solid #ebeef5; background: #f8f9fa; border-radius: 12px 12px 0 0; }
.panel-header h3 { margin: 0; font-size: 15px; color: #303133; }
.panel-content { flex: 1; overflow-y: auto; padding: 20px; }
.panel-section { margin-bottom: 24px; }
.section-title { font-size: 14px; color: #303133; margin-bottom: 16px; display: flex; align-items: center; gap: 6px; }
.evidence-header { display: flex; align-items: center; gap: 8px; width: 100%; }
.paper-title { flex: 1; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #606266; }
.page-num { font-size: 12px; color: #909399; }
.evidence-text { font-size: 13px; color: #606266; line-height: 1.6; background: #f8f9fa; padding: 12px; border-radius: 6px; }
@media (max-width: 1100px) { .side-panel { display: none; } }
</style>