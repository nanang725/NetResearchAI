
<template>
  <div class="qa-page">
    <section class="qa-toolbar page-card">
      <div>
        <p class="eyebrow">EVIDENCE-GROUNDED RESEARCH CHAT</p>
        <h2>基于馆藏证据的研究对话</h2>
        <p class="muted">
          每轮回答保留检索范围、执行步骤与可回溯论文证据；通用知识回答会明确区分。
        </p>
      </div>
      <div class="qa-status-actions">
        <div class="request-state" :class="requestState">
          <span class="state-dot"></span>
          <div>
            <strong>{{ requestStateLabel }}</strong>
            <small>{{ requestStateDetail }}</small>
          </div>
        </div>
        <el-button v-if="messages.length" plain @click="clearConversation">
          清空本地对话
        </el-button>
      </div>
    </section>

    <div class="qa-container">
    <!-- 左侧：聊天主区域 -->
    <div class="chat-main">
      <div class="chat-history" ref="chatHistoryRef">
        <div v-if="messages.length === 0" class="welcome-box">
          <div class="welcome-icon">
            <ChatDotRound />
          </div>
          <h3>NetResearch AI 科研助手</h3>
          <p>已就绪。您可以围绕已收录论文提问；我会在检索到相关证据时据此回答。也可在问题中说明希望使用通用知识，通用知识回答不代表论文证据。</p>
          <div class="prompt-suggestions" aria-label="建议问题">
            <button
              v-for="prompt in promptSuggestions"
              :key="prompt"
              type="button"
              @click="usePrompt(prompt)"
            >
              {{ prompt }}
            </button>
          </div>
        </div>

        <div
          v-for="(msg, index) in messages"
          :key="index"
          class="message-row"
          :class="msg.role"
        >
          <div class="avatar" :class="msg.role">
            {{ msg.role === 'user' ? '我' : msg.role === 'imported' ? 'GPT' : 'AI' }}
          </div>

          <div class="bubble">
            <span class="message-label">
              {{ msg.role === 'user' ? '你的问题' : msg.role === 'imported' ? '导入的 ChatGPT 对话' : 'NetResearch AI' }}
            </span>
            <div
              v-if="msg.role === 'ai'"
              class="markdown-body"
              v-html="renderMarkdown(msg.content)"
            ></div>

            <div v-else class="user-text">
              {{ msg.content }}
            </div>
          </div>
        </div>

        <div v-if="loading" class="message-row ai">
          <div class="avatar ai">AI</div>
          <div class="bubble loading">
            <el-icon class="is-loading">
              <Loading />
            </el-icon>
            <span>
              <strong>正在检索本地论文并生成回答</strong>
              <small>{{ loadingStage }} · {{ loadingElapsedText }}</small>
            </span>
          </div>
        </div>
      </div>

      <div class="input-box">
        <!-- 检索范围选择 -->
        <div class="scope-selector">
          <span class="scope-label">检索范围</span>

          <el-select
            v-model="selectedPaperId"
            placeholder="全库联合检索（默认）"
            clearable
            size="small"
            style="width: 250px"
          >
            <el-option
              label="全库联合检索（所有论文）"
              value=""
            />

            <el-option
              v-for="p in paperList"
              :key="p.id"
              :label="p.title"
              :value="p.id"
            />
          </el-select>
          <span class="scope-summary">
            {{ selectedPaperId === "" ? `${paperList.length} 篇本地论文联合检索` : "仅检索所选论文" }}
          </span>
        </div>

        <div class="quick-prompts">
          <button
            v-for="prompt in compactPrompts"
            :key="prompt"
            type="button"
            :disabled="loading"
            @click="usePrompt(prompt)"
          >
            {{ prompt }}
          </button>
        </div>

        <!-- 问题输入框 -->
        <el-input
          v-model="query"
          type="textarea"
          :autosize="{ minRows: 2, maxRows: 4 }"
          placeholder="输入您的问题（按 Enter 发送，Shift + Enter 换行）"
          @keydown.enter.prevent="handleEnter"
          class="chat-input"
        />

        <div class="input-actions">
          <span class="hint">
            v0.4.0 包含上下文问答；论文证据检索支持全库或单篇范围
          </span>

          <div class="action-buttons">
            <el-button
              :disabled="loading"
              @click="importDialogVisible = true"
            >
              导入 ChatGPT 对话
            </el-button>

            <el-button
              type="primary"
              :loading="loading"
              @click="sendMessage"
              round
            >
              发送
            </el-button>
          </div>
        </div>
      </div>

      <!-- 导入 ChatGPT 对话 -->
      <el-dialog
        v-model="importDialogVisible"
        title="导入 ChatGPT 对话"
        width="640px"
      >
        <p class="import-hint">
          粘贴从 ChatGPT 网页复制的对话内容。记录只保存在当前浏览器，不会上传到后端。
        </p>

        <el-input
          v-model="importedText"
          type="textarea"
          :autosize="{ minRows: 8, maxRows: 18 }"
          placeholder="在此粘贴 ChatGPT 对话..."
        />

        <template #footer>
          <el-button @click="importDialogVisible = false">
            取消
          </el-button>

          <el-button
            type="primary"
            @click="importConversation"
          >
            导入
          </el-button>
        </template>
      </el-dialog>
    </div>

    <!-- 右侧：Agent 思考与证据溯源面板 -->
    <div class="side-panel">
      <div class="panel-header">
        <div>
          <p class="eyebrow">AGENT TRACE</p>
          <h3>执行轨迹与证据</h3>
        </div>
        <el-tag v-if="currentContexts.length" effect="plain">
          {{ currentContexts.length }} 条证据
        </el-tag>
      </div>

      <div class="panel-content">
        <el-empty
          v-if="!currentSteps.length && currentIsRelevant === null"
          description="发送消息后，此处将展示执行过程与原文证据"
          :image-size="80"
        />

        <template v-else>
          <!-- Agent 执行步骤 -->
          <div
            v-if="currentSteps.length"
            class="panel-section"
          >
            <h4 class="section-title">
              <el-icon>
                <Guide />
              </el-icon>
              执行步骤
            </h4>

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

          <!-- 无关问题提示 -->
          <div
            v-if="currentIsRelevant === false && currentKnowledgeSource !== 'generic_knowledge'"
            class="irrelevant-hint"
          >
            <el-icon>
              <Warning />
            </el-icon>

            <span>
              当前没有可用于回答该问题的相关论文证据，已隐藏无关证据来源。
            </span>
          </div>

          <div
            v-if="currentKnowledgeSource === 'generic_knowledge'"
            class="no-evidence-hint"
          >
            本轮回答使用通用知识，不代表论文原文或论文证据。
          </div>

          <!-- 只有明确相关时才显示证据 -->
          <div
            v-if="currentIsRelevant === true && currentContexts.length"
            class="panel-section"
          >
            <h4 class="section-title">
              <el-icon>
                <Document />
              </el-icon>

              溯源证据 ({{ currentContexts.length }})
            </h4>

            <el-collapse v-model="activeEvidence">
              <el-collapse-item
                v-for="(ctx, idx) in currentContexts"
                :key="idx"
                :name="idx.toString()"
              >
                <template #title>
                  <div class="evidence-header">
                    <el-tag
                      size="small"
                      type="success"
                      effect="dark"
                    >
                      [{{ idx + 1 }}]
                    </el-tag>

                    <span class="paper-title">
                      {{ ctx.paper_title }}
                    </span>

                    <span class="page-num">
                      第 {{ ctx.page_number }} 页
                    </span>
                  </div>
                </template>

                <div class="evidence-text">
                  {{ ctx.text }}
                </div>
                <div class="evidence-actions">
                  <span class="mono">
                    chunk {{ ctx.chunk_id }} · {{ ctx.retrieval_method }}
                  </span>
                  <el-button link type="primary" @click="openEvidence(ctx)">
                    打开原文位置
                  </el-button>
                </div>
              </el-collapse-item>
            </el-collapse>
          </div>

          <!-- 相关但没有可展示证据 -->
          <div
            v-if="currentIsRelevant === true && !currentContexts.length"
            class="no-evidence-hint"
          >
            回答已生成，但当前没有可展示的证据来源。
          </div>
        </template>
      </div>
    </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, nextTick, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Loading,
  Guide,
  Document,
  Warning,
  ChatDotRound
} from '@element-plus/icons-vue'

import { marked } from 'marked'
import { chatWithAgent, fetchPapers, type ChatHistoryMessage } from '@/api/papers'

interface Message {
  role: 'user' | 'ai' | 'imported'
  content: string
}

interface Paper {
  id: number
  title: string
}

interface ChatResponse {
  answer?: string
  steps?: string[]
  contexts?: ChatContext[]
  is_relevant?: boolean
  knowledge_source?: 'paper_evidence' | 'generic_knowledge'
}

interface ChatContext {
  chunk_id: number
  paper_id: number
  paper_title: string
  page_number: number
  text: string
  retrieval_method: 'hybrid' | 'bm25' | 'vector' | string
  retrieval_score: number
}

type RequestState = 'idle' | 'loading' | 'success' | 'error'

const MESSAGE_STORAGE_KEY = 'netresearch-ai-qa-messages'
const MAX_HISTORY_MESSAGES = 12
const MAX_HISTORY_MESSAGE_CHARS = 2000
const router = useRouter()

// 检查聊天记录格式
const isMessage = (value: unknown): value is Message => {
  if (!value || typeof value !== 'object') {
    return false
  }

  const message = value as Record<string, unknown>

  return (
    ['user', 'ai', 'imported'].includes(String(message.role))
    && typeof message.content === 'string'
  )
}

// 读取本地聊天记录
const loadStoredMessages = (): Message[] => {
  try {
    const stored = localStorage.getItem(MESSAGE_STORAGE_KEY)

    if (!stored) {
      return []
    }

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

// 保存本地聊天记录
const persistMessages = (value: Message[]): boolean => {
  try {
    localStorage.setItem(
      MESSAGE_STORAGE_KEY,
      JSON.stringify(value)
    )

    return true
  } catch (error) {
    console.error('保存聊天记录失败', error)
    ElMessage.error('保存聊天记录失败，请检查浏览器存储空间。')
    return false
  }
}

// 基础状态
const query = ref('')
const loading = ref(false)
const requestState = ref<RequestState>('idle')
const requestError = ref('')
const loadingElapsedMs = ref(0)
const lastResponseDurationMs = ref<number | null>(null)
let loadingStartedAt = 0
let loadingTimer: ReturnType<typeof setInterval> | null = null

const messages = ref<Message[]>(loadStoredMessages())

const chatHistoryRef = ref<HTMLElement | null>(null)

const importDialogVisible = ref(false)
const importedText = ref('')

// 论文列表与检索范围
const paperList = ref<Paper[]>([])
const selectedPaperId = ref<number | string>('')

// Agent 右侧面板数据
const currentSteps = ref<string[]>([])
const currentContexts = ref<ChatContext[]>([])

// null：尚未获得相关性判断
// true：证据相关
// false：证据无关或未通过相关性判断
const currentIsRelevant = ref<boolean | null>(null)
const currentKnowledgeSource = ref<
  'paper_evidence' | 'generic_knowledge' | null
>(null)

const activeEvidence = ref<string[]>(['0'])

const promptSuggestions = [
  '总结当前论文库中最常讨论的研究问题',
  '比较这些论文使用的主要方法和实验设置',
  '哪些证据支持论文中的核心结论？',
]
const compactPrompts = ['总结核心观点', '比较研究方法', '查找证据局限']

const selectedScopeLabel = computed(() =>
  selectedPaperId.value === ''
    ? '全库联合检索'
    : paperList.value.find((paper) => paper.id === Number(selectedPaperId.value))?.title
      || '单篇论文检索',
)

const requestStateLabel = computed(() => {
  if (requestState.value === 'loading') return '正在执行';
  if (requestState.value === 'success') return '最近一轮已完成';
  if (requestState.value === 'error') return '最近一轮失败';
  return '等待提问';
})

const requestStateDetail = computed(() => {
  if (requestState.value === 'loading') return `${selectedScopeLabel.value} · ${loadingElapsedText.value}`;
  if (requestState.value === 'success') {
    const duration = lastResponseDurationMs.value === null
      ? ''
      : ` · 实测 ${(lastResponseDurationMs.value / 1000).toFixed(1)} s`;
    return `${currentContexts.value.length} 条证据${duration}`;
  }
  if (requestState.value === 'error') return requestError.value || '请求未完成';
  return '问题将发送到当前服务';
})

const loadingElapsedText = computed(
  () => `${(loadingElapsedMs.value / 1000).toFixed(1)} s`,
)

const loadingStage = computed(() =>
  loadingElapsedMs.value >= 8000
    ? '复杂检索仍在处理，请保持页面打开'
    : '正在检索证据并组织回答',
)

// 页面加载时获取论文列表
onMounted(async () => {
  try {
    const res: any = await fetchPapers()

    if (Array.isArray(res)) {
      paperList.value = res
    } else if (res && Array.isArray(res.data)) {
      paperList.value = res.data
    }
  } catch (error) {
    console.error('获取论文列表失败', error)
  }
})

// Markdown 渲染
const renderMarkdown = (text: string) => {
  if (!text) {
    return ''
  }

  return marked.parse(text)
}

// Enter 发送，Shift + Enter 换行
const handleEnter = (e: KeyboardEvent) => {
  if (!e.shiftKey) {
    sendMessage()
  }
}

// 滚动到聊天底部
const scrollToBottom = async () => {
  await nextTick()

  if (chatHistoryRef.value) {
    chatHistoryRef.value.scrollTop =
      chatHistoryRef.value.scrollHeight
  }
}

const startLoadingClock = () => {
  loadingStartedAt = performance.now()
  loadingElapsedMs.value = 0
  if (loadingTimer) clearInterval(loadingTimer)
  loadingTimer = setInterval(() => {
    loadingElapsedMs.value = performance.now() - loadingStartedAt
  }, 100)
}

const stopLoadingClock = () => {
  if (loadingTimer) clearInterval(loadingTimer)
  loadingTimer = null
  if (loadingStartedAt) {
    loadingElapsedMs.value = performance.now() - loadingStartedAt
  }
}

const usePrompt = (prompt: string) => {
  if (loading.value) return
  query.value = prompt
  void sendMessage()
}

const clearConversation = async () => {
  try {
    await ElMessageBox.confirm(
      '将清空当前浏览器保存的对话、执行轨迹与证据展示。该操作不会删除论文或服务端数据。',
      '清空本地对话',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  messages.value = []
  currentSteps.value = []
  currentContexts.value = []
  currentIsRelevant.value = null
  currentKnowledgeSource.value = null
  requestState.value = 'idle'
  requestError.value = ''
  lastResponseDurationMs.value = null
  try {
    localStorage.removeItem(MESSAGE_STORAGE_KEY)
  } catch {
    ElMessage.warning('对话已清空，但浏览器存储未能同步清理。')
  }
}

const openEvidence = (context: ChatContext) => {
  router.push({
    name: 'paper-detail',
    params: { id: context.paper_id },
    query: { page: String(context.page_number) },
  })
}

const buildRequestHistory = (): ChatHistoryMessage[] =>
  messages.value
    .filter((message) => message.role === 'user' || message.role === 'ai')
    .slice(-MAX_HISTORY_MESSAGES)
    .map((message) => ({
      role: message.role === 'ai' ? 'assistant' : 'user',
      content: message.content.slice(0, MAX_HISTORY_MESSAGE_CHARS),
    }))

// 导入 ChatGPT 对话
const importConversation = () => {
  const content = importedText.value.trim()

  if (!content) {
    ElMessage.warning('请先粘贴 ChatGPT 对话内容。')
    return
  }

  messages.value.push({
    role: 'imported',
    content
  })

  const saved = persistMessages(messages.value)

  importedText.value = ''
  importDialogVisible.value = false

  if (saved) {
    ElMessage.success(
      'ChatGPT 对话已导入并保存在当前浏览器。'
    )
  }

  scrollToBottom()
}

// 发送问题
const sendMessage = async () => {
  const q = query.value.trim()

  if (!q || loading.value) {
    return
  }

  const history = buildRequestHistory()

  // 添加用户消息
  messages.value.push({
    role: 'user',
    content: q
  })

  persistMessages(messages.value)

  query.value = ''
  loading.value = true
  requestState.value = 'loading'
  requestError.value = ''
  startLoadingClock()

  // 新请求开始时清除上一次的执行轨迹和证据
  currentSteps.value = []
  currentContexts.value = []
  currentIsRelevant.value = null
  currentKnowledgeSource.value = null
  activeEvidence.value = ['0']

  scrollToBottom()

  try {
    // 空字符串表示全库检索
    const paperIdToSend =
      selectedPaperId.value === ''
        ? undefined
        : Number(selectedPaperId.value)

    const res: any = await chatWithAgent(
      q,
      paperIdToSend,
      history
    )

    // 兼容直接返回和 Axios data 包装两种情况
    const data: ChatResponse =
      res?.answer
        ? res
        : (res?.data || res)

    // 添加 AI 回答
    messages.value.push({
      role: 'ai',
      content: data.answer || '未获取到回答'
    })

    persistMessages(messages.value)

    // 更新 Agent 执行步骤
    currentSteps.value = Array.isArray(data.steps)
      ? data.steps
      : []

    // 只有后端明确返回 true 才展示证据
    currentIsRelevant.value =
      data.is_relevant === true
    currentKnowledgeSource.value =
      data.knowledge_source === 'generic_knowledge'
        ? 'generic_knowledge'
        : 'paper_evidence'

    currentContexts.value =
      currentIsRelevant.value && Array.isArray(data.contexts)
        ? data.contexts
        : []
    requestState.value = 'success'

    // 重置证据展开状态
    activeEvidence.value =
      currentContexts.value.length > 0
        ? ['0']
        : []

  } catch (error) {
    console.error('智能问答请求失败', error)

    ElMessage.error(
      '请求失败，请检查网络或后端日志。'
    )
    requestState.value = 'error'
    requestError.value = error instanceof Error ? error.message : '请求失败'

    messages.value.push({
      role: 'ai',
      content: '网络异常，生成失败。'
    })

    persistMessages(messages.value)

    // 请求失败时不保留旧证据
    currentSteps.value = []
    currentContexts.value = []
    currentIsRelevant.value = null
    currentKnowledgeSource.value = null

  } finally {
    stopLoadingClock()
    lastResponseDurationMs.value = loadingElapsedMs.value
    loading.value = false
    scrollToBottom()
  }
}

onUnmounted(() => {
  if (loadingTimer) clearInterval(loadingTimer)
})
</script>

<style scoped>
.qa-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.qa-toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 18px 20px;
}

.qa-toolbar h2 {
  margin: 4px 0 5px;
  font-size: 21px;
  letter-spacing: -0.025em;
}

.qa-toolbar p {
  margin: 0;
}

.qa-status-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 10px;
}

.request-state {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
}

.request-state > div {
  display: flex;
  flex-direction: column;
}

.request-state strong {
  color: var(--color-text-primary);
  font-size: 11px;
}

.request-state small {
  max-width: 250px;
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.state-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  background: var(--color-text-tertiary);
  border-radius: 50%;
}

.request-state.loading .state-dot {
  background: var(--color-warning);
  box-shadow: 0 0 0 4px var(--color-warning-soft);
  animation: state-pulse 1.3s ease-in-out infinite;
}

.request-state.success .state-dot {
  background: var(--color-success);
}

.request-state.error .state-dot {
  background: var(--color-danger);
}

@keyframes state-pulse {
  50% {
    opacity: 0.45;
  }
}

.qa-container {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 360px;
  height: calc(100vh - 250px);
  min-height: 620px;
  gap: 16px;
}

.chat-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border-radius: 12px;
  box-shadow: var(--shadow-xs);
  border: 1px solid var(--color-border-subtle);
  overflow: hidden;
}

.chat-history {
  flex: 1;
  padding: 24px;
  overflow-y: auto;
  background: var(--color-bg-subtle);
}

.welcome-box {
  text-align: center;
  max-width: 680px;
  margin: 58px auto 0;
  color: var(--color-text-secondary);
}

.welcome-icon {
  display: grid;
  width: 46px;
  height: 46px;
  margin: 0 auto 14px;
  place-items: center;
  color: var(--color-primary);
  background: var(--color-primary-soft);
  border-radius: var(--radius-lg);
}

.welcome-icon svg {
  width: 22px;
}

.welcome-box h3 {
  color: var(--color-text-primary);
  margin: 0 0 8px;
}

.welcome-box > p {
  margin: 0;
  line-height: 1.7;
}

.prompt-suggestions {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: 24px;
}

.prompt-suggestions button {
  padding: 12px;
  color: var(--color-text-secondary);
  text-align: left;
  cursor: pointer;
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  font-size: 12px;
  line-height: 1.55;
}

.prompt-suggestions button:hover {
  color: var(--color-primary);
  border-color: var(--color-primary-border);
}

.message-row {
  display: flex;
  margin-bottom: 24px;
  gap: 12px;
}

.message-row.user {
  flex-direction: row-reverse;
}

.avatar {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: bold;
  flex-shrink: 0;
}

.avatar.ai {
  background: var(--color-primary-soft);
  color: var(--color-primary);
}

.avatar.user {
  background: var(--color-primary);
  color: var(--color-text-inverse);
}

.avatar.imported {
  background: var(--color-success-soft);
  color: var(--color-success);
}

.bubble {
  position: relative;
  max-width: 80%;
  padding: 12px 16px;
  border-radius: 12px;
  font-size: 14px;
  line-height: 1.6;
}

.message-label {
  display: block;
  margin-bottom: 5px;
  color: var(--color-text-tertiary);
  font-size: 9px;
  font-weight: 650;
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.message-row.user .message-label {
  color: color-mix(in srgb, var(--color-text-inverse) 70%, transparent);
}

.message-row.ai .bubble {
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-top-left-radius: 2px;
}

.message-row.user .bubble {
  background: var(--color-primary);
  color: var(--color-text-inverse);
  border-top-right-radius: 2px;
}

.message-row.imported .bubble {
  background: var(--color-surface);
  border: 1px solid var(--color-primary-border);
  border-top-left-radius: 2px;
}

.user-text {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.bubble.loading {
  color: var(--color-text-tertiary);
  display: flex;
  align-items: center;
  gap: 8px;
}

.bubble.loading > span {
  display: flex;
  flex-direction: column;
}

.bubble.loading strong {
  color: var(--color-text-primary);
  font-size: 12px;
}

.bubble.loading small {
  font-size: 10px;
}

.bubble.loading :deep(.el-icon) {
  flex: 0 0 auto;
}

:deep(.markdown-body p) {
  margin: 0 0 10px;
}

:deep(.markdown-body h1),
:deep(.markdown-body h2),
:deep(.markdown-body h3) {
  margin: 16px 0 8px;
  font-size: 16px;
}

:deep(.markdown-body ul) {
  padding-left: 20px;
  margin-bottom: 10px;
}

:deep(.markdown-body li) {
  margin-bottom: 4px;
}

:deep(.markdown-body strong) {
  color: var(--color-text-primary);
}

:deep(.markdown-body pre),
:deep(.markdown-body code) {
  background: var(--color-bg-subtle);
  border-radius: var(--radius-sm);
}

:deep(.markdown-body pre) {
  overflow-x: auto;
  padding: 10px;
}

:deep(.markdown-body table) {
  width: 100%;
  border-collapse: collapse;
}

:deep(.markdown-body td),
:deep(.markdown-body th) {
  padding: 6px 8px;
  border: 1px solid var(--color-border-default);
}

.input-box {
  padding: 16px 24px;
  background: var(--color-surface);
  border-top: 1px solid var(--color-border-subtle);
}

.scope-selector {
  display: flex;
  align-items: center;
  margin-bottom: 12px;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.scope-label {
  font-weight: bold;
  margin-right: 8px;
}

.scope-summary {
  margin-left: 10px;
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.quick-prompts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 9px;
}

.quick-prompts button {
  padding: 3px 8px;
  color: var(--color-text-secondary);
  cursor: pointer;
  background: var(--color-bg-subtle);
  border: 1px solid transparent;
  border-radius: var(--radius-pill);
  font-size: 10px;
}

.quick-prompts button:hover {
  color: var(--color-primary);
  border-color: var(--color-primary-border);
}

.chat-input :deep(.el-textarea__inner) {
  box-shadow: none;
  padding: 12px;
  background: var(--color-bg-subtle);
  border-radius: 8px;
  font-size: 14px;
}

.chat-input :deep(.el-textarea__inner:focus) {
  background: var(--color-surface);
  box-shadow: 0 0 0 1px var(--color-primary) inset;
}

.input-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}

.action-buttons {
  display: flex;
  gap: 8px;
}

.hint {
  font-size: 12px;
  color: var(--color-text-tertiary);
}

.import-hint {
  margin: 0 0 12px;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.side-panel {
  min-width: 0;
  background: var(--color-surface);
  border-radius: 12px;
  box-shadow: var(--shadow-xs);
  border: 1px solid var(--color-border-subtle);
  display: flex;
  flex-direction: column;
}

.panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border-subtle);
  background: var(--color-bg-subtle);
  border-radius: 12px 12px 0 0;
}

.panel-header h3 {
  margin: 3px 0 0;
  font-size: 15px;
  color: var(--color-text-primary);
}

.panel-content {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}

.panel-section {
  margin-bottom: 24px;
}

.section-title {
  font-size: 14px;
  color: var(--color-text-primary);
  margin-bottom: 16px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.evidence-header {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
}

.paper-title {
  flex: 1;
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--color-text-secondary);
}

.page-num {
  font-size: 12px;
  color: var(--color-text-tertiary);
}

.evidence-text {
  font-size: 13px;
  color: var(--color-text-secondary);
  line-height: 1.6;
  background: var(--color-bg-subtle);
  padding: 12px;
  border-radius: 6px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.evidence-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
  color: var(--color-text-tertiary);
  font-size: 9px;
}

.irrelevant-hint {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 12px;
  margin-bottom: 16px;
  border-radius: 6px;
  background: var(--color-warning-soft);
  color: var(--color-warning);
  font-size: 13px;
  line-height: 1.6;
}

.no-evidence-hint {
  color: var(--color-text-tertiary);
  font-size: 13px;
  padding: 8px 0;
}

.qa-container :deep(.el-collapse) {
  border-color: var(--color-border-subtle);
}

@media (max-width: 1100px) {
  .qa-container {
    grid-template-columns: 1fr;
    height: auto;
    min-height: 0;
  }

  .chat-main {
    min-height: 68vh;
  }

  .side-panel {
    width: auto;
    min-height: 360px;
  }
}

@media (max-width: 720px) {
  .qa-toolbar,
  .qa-status-actions,
  .input-actions,
  .scope-selector {
    align-items: stretch;
    flex-direction: column;
  }

  .qa-status-actions {
    width: 100%;
  }

  .request-state small {
    max-width: none;
  }

  .chat-history {
    padding: 15px;
  }

  .welcome-box {
    margin-top: 24px;
  }

  .prompt-suggestions {
    grid-template-columns: 1fr;
  }

  .bubble {
    max-width: calc(100% - 44px);
  }

  .input-box {
    padding: 14px 15px;
  }

  .scope-selector :deep(.el-select) {
    width: 100% !important;
  }

  .scope-summary {
    margin: 5px 0 0;
  }
}
</style>
