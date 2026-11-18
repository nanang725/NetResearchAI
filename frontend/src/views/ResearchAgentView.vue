<template>
  <div class="agent-page">
    <section class="hero page-card">
      <div>
        <p class="eyebrow">V0.4.0 · PERSISTENT RESEARCH AGENT</p>
        <h2>从研究目标到可核验报告</h2>
        <p class="muted">
          Agent 会持久化计划、按依赖调度只读工具，并依据中间检索结果触发一次受控重规划。
          外部 arXiv 摘要不会被标注为全文页码证据。
        </p>
      </div>
      <div class="dimensions">
        <el-tag effect="plain" type="success">v0.4.0 正式版本</el-tag>
        <el-tag effect="plain" type="success">只读工具白名单</el-tag>
      </div>
    </section>

    <section class="goal-card page-card">
      <div class="goal-heading">
        <div>
          <p class="eyebrow">RESEARCH OBJECTIVE</p>
          <h3>定义本轮研究目标</h3>
          <p class="muted">
            目标越具体，计划节点与证据检索越容易核验。当前执行限制为只读工具。
          </p>
        </div>
        <el-tag effect="plain" type="success">v0.4.0 正式版本</el-tag>
      </div>
      <el-input
        v-model="goal"
        type="textarea"
        :rows="4"
        maxlength="2000"
        show-word-limit
        placeholder="例如：比较图神经网络与 Transformer 在网络流量预测中的方法、实验结论与局限"
        @keydown.ctrl.enter="createPlan"
      />
      <div class="goal-actions">
        <span class="muted">
          预算：8 节点 / 12 次工具调用 / 3 次 arXiv 请求 / 180 秒
        </span>
        <div class="goal-buttons">
          <el-button
            plain
            :loading="creating"
            :disabled="goal.trim().length < 5 || planCreating"
            @click="createRunAutomatically"
          >
            旧模式直接执行
          </el-button>
          <el-button
            type="primary"
            :loading="planCreating"
            :disabled="goal.trim().length < 5 || creating"
            @click="createPlan"
          >
            生成研究计划
          </el-button>
        </div>
      </div>
      <el-alert
        v-if="error"
        :title="error"
        type="error"
        show-icon
        :closable="false"
      />
    </section>

    <section v-if="!run && !creating" class="agent-empty page-card">
      <div class="empty-mark"><Cpu /></div>
      <div>
        <h3>从目标到可核验执行轨迹</h3>
        <p>
          Agent 会持久化计划、按依赖调度工具，并将本地全文证据与 arXiv 外部摘要分开标注。
          计划失败、重试和取消状态会保留在实际执行记录中。
        </p>
        <div class="capability-list">
          <span>只读工具白名单</span>
          <span>运行时证据校验</span>
          <span>受控动态重规划</span>
          <span>最大 180 秒</span>
        </div>
        <ol class="agent-pipeline" aria-label="Agent 执行阶段">
          <li><strong>Planner</strong><span>生成可编辑计划与依赖关系</span></li>
          <li><strong>Executor</strong><span>按预算调用只读工具并记录轨迹</span></li>
          <li><strong>Evidence</strong><span>校验本地 Chunk 或标记 arXiv 摘要</span></li>
          <li><strong>Report</strong><span>汇总引用证据并保留可恢复任务</span></li>
        </ol>
      </div>
    </section>

    <template v-if="run">
      <section class="overview page-card">
        <div class="overview-main">
          <div class="status-line">
            <el-tag :type="statusType(run.status)">{{ statusLabel(run.status) }}</el-tag>
            <el-tag v-if="run.plan" effect="plain">
              {{ planSourceLabel(run.plan.generated_by) }}
            </el-tag>
            <el-tag v-if="run.plan?.edited_by_user" type="warning" effect="plain">
              用户已调整
            </el-tag>
            <el-tag v-if="run.plan" effect="plain">
              计划 v{{ run.plan.version }}
            </el-tag>
            <span class="run-id mono">{{ run.run_id }}</span>
          </div>
          <h3>{{ run.topic || "正在理解研究目标…" }}</h3>
          <p class="muted">{{ run.research_question || run.goal }}</p>
          <div class="dimensions">
            <el-tag
              v-for="dimension in run.analysis_dimensions"
              :key="dimension"
              effect="plain"
            >
              {{ dimension }}
            </el-tag>
          </div>
          <div v-if="run.plan_estimate" class="plan-estimate">
            <span>预计 {{ run.plan_estimate.node_count }} 节点</span>
            <span>{{ run.plan_estimate.tool_call_estimate }} 次工具调用</span>
            <span>{{ run.plan_estimate.arxiv_request_estimate }} 次 arXiv</span>
            <span>{{ run.plan_estimate.llm_call_estimate }} 次工具内 LLM 调用</span>
          </div>
          <div class="worker-state">
            <el-tag
              size="small"
              effect="plain"
              :type="run.worker_state.lease_status === 'active' ? 'success' : 'info'"
            >
              Worker 租约 {{ run.worker_state.lease_status }}
            </el-tag>
            <span>Generation {{ run.worker_state.lease_generation }}</span>
            <span>恢复 {{ run.worker_state.resume_count }} 次</span>
            <span v-if="run.worker_state.heartbeat_at">
              最近心跳 {{ formatDateTime(run.worker_state.heartbeat_at) }}
            </span>
          </div>
          <div class="run-progress">
            <div>
              <span>计划节点完成度</span>
              <strong>{{ completedNodeCount }} / {{ run.nodes.length || run.budget.max_nodes }}</strong>
            </div>
            <el-progress
              :percentage="nodeCompletionPercent"
              :show-text="false"
              :stroke-width="6"
            />
          </div>
        </div>
        <div class="metrics">
          <div><strong>{{ run.usage.tool_calls }}</strong><span>工具调用</span></div>
          <div><strong>{{ run.usage.llm_calls }}</strong><span>LLM 调用</span></div>
          <div><strong>{{ run.usage.arxiv_requests }}</strong><span>arXiv 请求</span></div>
          <div><strong>{{ run.replan_count }}</strong><span>动态重规划</span></div>
        </div>
        <div v-if="isDraft" class="draft-actions">
          <el-button plain @click="openPlanEditor">编辑计划</el-button>
          <el-button
            type="primary"
            :loading="confirmingPlan"
            @click="confirmPlan"
          >
            确认并开始执行
          </el-button>
        </div>
        <el-button
          v-else-if="canCancel"
          type="danger"
          plain
          :loading="cancelling"
          @click="cancelRun"
        >
          取消任务
        </el-button>
      </section>

      <el-alert
        v-if="isDraft"
        title="计划尚未启动"
        description="确认前不会创建执行线程。确认后由现有 Executor 按该版本执行。"
        type="info"
        show-icon
        :closable="false"
      />

      <el-collapse v-if="run.plan_history.length" class="page-card plan-history">
        <el-collapse-item
          :title="`计划变更记录（${run.plan_history.length} 个版本）`"
          name="history"
        >
          <article
            v-for="entry in run.plan_history"
            :key="`${entry.version}-${entry.created_at}`"
          >
            <div>
              <strong>v{{ entry.version }}</strong>
              <el-tag size="small" effect="plain">
                {{ planHistoryActionLabel(entry.action) }}
              </el-tag>
            </div>
            <p>{{ entry.change_reason }}</p>
            <small>{{ formatDateTime(entry.created_at) }}</small>
          </article>
        </el-collapse-item>
      </el-collapse>

      <el-collapse
        v-if="run.replan_decisions.length"
        class="page-card execution-history"
      >
        <el-collapse-item
          :title="`动态重规划记录（${run.replan_decisions.length}）`"
          name="replans"
        >
          <article
            v-for="(decision, index) in run.replan_decisions"
            :key="`${decision.created_at}-${index}`"
          >
            <div>
              <strong>
                v{{ decision.old_plan_version }}
                → {{ decision.new_plan_version ?? "拒绝" }}
              </strong>
              <el-tag
                size="small"
                :type="decision.accepted ? 'success' : 'warning'"
              >
                {{ decision.accepted ? "已应用" : "未应用" }}
              </el-tag>
            </div>
            <p>{{ decision.reason }}</p>
            <small>
              {{ decision.planner_source }} ·
              新增 {{ decision.added_nodes.length }} 节点 ·
              {{ formatDateTime(decision.created_at) }}
            </small>
          </article>
        </el-collapse-item>
      </el-collapse>

      <el-collapse
        v-if="run.recoveries.length"
        class="page-card execution-history"
      >
        <el-collapse-item
          :title="`任务恢复记录（${run.recoveries.length}）`"
          name="recoveries"
        >
          <article
            v-for="(recovery, index) in run.recoveries"
            :key="`${recovery.created_at}-${index}`"
          >
            <div>
              <strong>{{ recovery.reason }}</strong>
              <el-tag size="small" effect="plain">
                Generation {{ recovery.generation }}
              </el-tag>
            </div>
            <p>
              复用完成节点：{{ recovery.completed_nodes_reused.join(", ") || "无" }}
            </p>
            <p>
              待继续节点：{{ recovery.unfinished_nodes.join(", ") || "无" }}
            </p>
            <small>{{ formatDateTime(recovery.created_at) }}</small>
          </article>
        </el-collapse-item>
      </el-collapse>

      <el-alert
        v-if="run.error"
        :title="run.error"
        type="error"
        show-icon
        :closable="false"
      />

      <section class="grid">
        <div class="page-card panel">
          <div class="panel-title">
            <div>
              <h3>持久化研究计划</h3>
              <p class="muted">依赖已满足的节点才会启动。</p>
            </div>
            <div class="plan-tags">
              <el-tag effect="plain">计划 v{{ run.plan_version || 0 }}</el-tag>
              <el-tag type="success" effect="plain">{{ completedNodeCount }} 完成</el-tag>
              <el-tag v-if="failedNodeCount" type="danger" effect="plain">
                {{ failedNodeCount }} 失败
              </el-tag>
              <el-tag v-if="run.replan_count" type="warning" effect="plain">
                {{ run.replan_count }} 次重规划
              </el-tag>
            </div>
          </div>
          <div v-if="run.nodes.length" class="node-list">
            <article
              v-for="node in run.nodes"
              :key="node.id"
              class="node"
              :class="node.status"
            >
              <div class="node-head">
                <div>
                  <span class="node-id mono">{{ node.node_key }}</span>
                  <strong>{{ node.tool_name }}</strong>
                </div>
                <el-tag size="small" :type="statusType(node.status)">
                  {{ statusLabel(node.status) }}
                </el-tag>
                <el-tag
                  v-if="node.structured_output"
                  size="small"
                  effect="plain"
                  :type="toolStatusType(node.structured_output.output.status)"
                >
                  {{ toolStatusLabel(node.structured_output.output.status) }}
                </el-tag>
                <el-tag
                  v-if="node.strategy_decision"
                  size="small"
                  effect="plain"
                  type="warning"
                >
                  {{ strategyLabel(node.strategy_decision.strategy) }}
                </el-tag>
              </div>
              <p>{{ node.reason }}</p>
              <div class="node-meta">
                <span>依赖：{{ node.depends_on.join(", ") || "无" }}</span>
                <span>重试：{{ node.retry_count }} / {{ node.retry_limit }}</span>
              </div>
              <el-collapse class="node-details">
                <el-collapse-item title="输入、输出与完成条件" :name="node.id">
                  <dl>
                    <div>
                      <dt>原始计划参数</dt>
                      <dd class="mono">{{ JSON.stringify(node.input) }}</dd>
                    </div>
                    <div>
                      <dt>最终执行参数</dt>
                      <dd class="mono">
                        {{ JSON.stringify(node.execution_input?.resolved_arguments ?? node.input) }}
                      </dd>
                    </div>
                    <div v-if="node.execution_input?.binding_sources.length">
                      <dt>数据绑定来源</dt>
                      <dd>
                        <ul class="binding-list">
                          <li
                            v-for="binding in node.execution_input.binding_sources"
                            :key="`${binding.argument}-${binding.source_node_id}-${binding.output_key}`"
                          >
                            <strong>{{ binding.argument }}</strong>
                            ← {{ binding.source_node_id }}.{{ binding.output_key }}
                            <span v-if="binding.item_count !== null">
                              （{{ binding.item_count }} 项）
                            </span>
                          </li>
                        </ul>
                      </dd>
                    </div>
                    <div>
                      <dt>完成条件</dt>
                      <dd>{{ node.completion_condition }}</dd>
                    </div>
                    <div>
                      <dt>输出定义</dt>
                      <dd>
                        {{ planNodeFor(node.node_key)?.output_definition.description || "由工具输出 Schema 定义" }}
                      </dd>
                    </div>
                    <div>
                      <dt>失败策略</dt>
                      <dd>
                        {{ planNodeFor(node.node_key)?.failure_strategy === "retry_then_fail" ? "重试后终止任务" : "工具默认策略" }}
                      </dd>
                    </div>
                    <div v-if="node.structured_output">
                      <dt>结构化输出</dt>
                      <dd>
                        <pre class="node-json-output">{{ JSON.stringify(node.structured_output.output, null, 2) }}</pre>
                      </dd>
                    </div>
                    <div v-if="node.error_output">
                      <dt>错误分类</dt>
                      <dd class="node-error">
                        {{ errorCategoryLabel(node.error_output.error.category) }}
                        · {{ node.error_output.error.code }}
                        <br />
                        {{ node.error_output.error.message }}
                      </dd>
                    </div>
                  </dl>
                </el-collapse-item>
              </el-collapse>
              <p v-if="node.error" class="node-error">{{ node.error }}</p>
            </article>
          </div>
          <el-empty v-else description="Planner 正在生成计划" :image-size="70" />
        </div>

        <div class="page-card panel">
          <div class="panel-title">
            <div>
              <h3>运行时证据</h3>
              <p class="muted">每条本地证据都由 SQLite 校验论文、页码和 Chunk。</p>
            </div>
            <el-tag effect="plain">{{ validEvidence.length }} 条有效</el-tag>
          </div>
          <div v-if="run.evidence.length" class="evidence-summary">
            <span>本地全文 {{ localEvidenceCount }}</span>
            <span>arXiv 摘要 {{ externalEvidenceCount }}</span>
            <span>校验失败 {{ invalidEvidenceCount }}</span>
          </div>
          <div v-if="run.evidence.length" class="evidence-list">
            <article
              v-for="item in run.evidence"
              :key="item.id"
              class="evidence"
              :class="{ invalid: !item.is_valid }"
            >
              <div class="evidence-head">
                <el-tag
                  size="small"
                  :type="item.source_type === 'local_full_text' ? 'success' : 'warning'"
                >
                  {{ evidenceLabel(item.source_type) }}
                </el-tag>
                <span class="mono">{{ evidenceLocator(item) }}</span>
              </div>
              <strong>{{ item.title || item.arxiv_id }}</strong>
              <p>{{ item.excerpt }}</p>
              <p v-if="item.validation_error" class="node-error">
                {{ item.validation_error }}
              </p>
            </article>
          </div>
          <el-empty v-else description="等待工具返回证据" :image-size="70" />
        </div>
      </section>

      <section class="page-card panel">
        <div class="panel-title">
          <div>
            <h3>工具调用与执行理由</h3>
            <p class="muted">失败调用、重试次数和耗时均持久化。</p>
          </div>
        </div>
        <el-table :data="run.tool_calls" size="small">
          <el-table-column prop="tool_name" label="工具" min-width="170" />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="statusType(row.status)">
                {{ statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="attempt" label="尝试" width="70" />
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">
              {{ row.duration_ms == null ? "—" : `${row.duration_ms} ms` }}
            </template>
          </el-table-column>
          <el-table-column label="结果摘要" min-width="260">
            <template #default="{ row }">
              <span class="mono summary">
                {{ row.result_summary ? JSON.stringify(row.result_summary) : row.error || "执行中" }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="执行理由" min-width="320">
            <template #default="{ row }">
              {{ reasonForCall(row.node_id) }}
            </template>
          </el-table-column>
        </el-table>
      </section>

      <section v-if="report" class="page-card report">
        <div class="panel-title">
          <div>
            <p class="eyebrow">FINAL ARTIFACT</p>
            <h3>{{ report.report.report?.title }}</h3>
          </div>
          <el-tag type="success">{{ report.valid_source_count }} 条有效来源</el-tag>
        </div>
        <p class="summary-text">{{ report.report.report?.executive_summary }}</p>
        <article
          v-for="section in report.report.report?.sections || []"
          :key="section.heading"
          class="report-section"
        >
          <h4>{{ section.heading }}</h4>
          <p>{{ section.content }}</p>
          <span class="mono">{{ section.citation_ids.join(", ") }}</span>
        </article>
        <div class="limitations">
          <strong>局限</strong>
          <p>{{ report.report.report?.limitations }}</p>
        </div>
      </section>
    </template>

    <el-dialog
      v-model="planEditorOpen"
      title="编辑研究计划草案"
      width="1000px"
      destroy-on-close
    >
      <div v-if="planForm" class="plan-editor">
        <el-form label-position="top">
          <div class="editor-grid">
            <el-form-item label="研究目标">
              <el-input
                v-model="planForm.goal"
                type="textarea"
                :rows="2"
                maxlength="2000"
                show-word-limit
              />
            </el-form-item>
            <el-form-item label="研究主题">
              <el-input v-model="planForm.topic" maxlength="200" />
            </el-form-item>
            <el-form-item label="核心研究问题">
              <el-input
                v-model="planForm.research_question"
                type="textarea"
                :rows="2"
                maxlength="1000"
              />
            </el-form-item>
            <el-form-item label="分析维度（逗号分隔）">
              <el-input v-model="planForm.analysisDimensionsText" />
            </el-form-item>
            <el-form-item label="时间范围">
              <el-input v-model="planForm.timeRange" placeholder="可留空" />
            </el-form-item>
            <el-form-item label="变更说明">
              <el-input v-model="planForm.changeReason" maxlength="500" />
            </el-form-item>
          </div>
        </el-form>

        <section class="budget-editor">
          <h4>执行预算</h4>
          <div>
            <label>
              <span>节点</span>
              <el-input-number v-model="planForm.budget.max_nodes" :min="1" :max="8" />
            </label>
            <label>
              <span>工具调用</span>
              <el-input-number
                v-model="planForm.budget.max_tool_calls"
                :min="1"
                :max="12"
              />
            </label>
            <label>
              <span>LLM 调用</span>
              <el-input-number
                v-model="planForm.budget.max_llm_calls"
                :min="0"
                :max="4"
              />
            </label>
            <label>
              <span>arXiv 请求</span>
              <el-input-number
                v-model="planForm.budget.max_arxiv_requests"
                :min="0"
                :max="3"
              />
            </label>
            <label>
              <span>重规划次数</span>
              <el-input-number
                v-model="planForm.budget.max_replans"
                :min="0"
                :max="3"
              />
            </label>
            <label>
              <span>最长秒数</span>
              <el-input-number
                v-model="planForm.budget.max_execution_seconds"
                :min="10"
                :max="600"
              />
            </label>
          </div>
        </section>

        <div class="node-editor-heading">
          <div>
            <h4>计划节点</h4>
            <p class="muted">工具、参数、依赖和预算将在保存时重新经过后端安全校验。</p>
          </div>
          <el-button plain @click="addPlanNode">添加节点</el-button>
        </div>

        <div class="node-editor-list">
          <article
            v-for="(node, index) in planForm.nodes"
            :key="index"
            class="node-editor-card"
          >
            <div class="node-editor-title">
              <strong>节点 {{ index + 1 }}</strong>
              <el-button
                link
                type="danger"
                :disabled="planForm.nodes.length === 1"
                @click="removePlanNode(index)"
              >
                删除
              </el-button>
            </div>
            <div class="node-editor-grid">
              <label>
                <span>节点 ID</span>
                <el-input v-model="node.id" maxlength="80" />
              </label>
              <label>
                <span>工具</span>
                <el-select v-model="node.tool_name" filterable>
                  <el-option
                    v-for="tool in toolCatalog"
                    :key="tool.name"
                    :label="tool.name"
                    :value="tool.name"
                  >
                    <div class="tool-option">
                      <strong>{{ tool.name }}</strong>
                      <small>{{ tool.description }}</small>
                    </div>
                  </el-option>
                </el-select>
              </label>
              <label>
                <span>依赖节点</span>
                <el-select v-model="node.depends_on" multiple collapse-tags>
                  <el-option
                    v-for="candidate in planForm.nodes.filter((_, candidateIndex) => candidateIndex !== index)"
                    :key="candidate.id"
                    :label="candidate.id"
                    :value="candidate.id"
                    :disabled="!candidate.id"
                  />
                </el-select>
              </label>
              <label>
                <span>重试上限</span>
                <el-input-number v-model="node.retry_limit" :min="0" :max="2" />
              </label>
              <label>
                <span>失败策略</span>
                <el-select v-model="node.failure_strategy">
                  <el-option label="重试后终止" value="retry_then_fail" />
                  <el-option
                    label="跳过非关键节点"
                    value="skip"
                    :disabled="node.critical"
                  />
                  <el-option label="使用替代工具" value="fallback_tool" />
                </el-select>
              </label>
              <label class="critical-field">
                <span>关键性</span>
                <el-checkbox v-model="node.critical">关键研究节点</el-checkbox>
              </label>
              <label v-if="node.failure_strategy === 'fallback_tool'">
                <span>替代工具</span>
                <el-select v-model="node.fallback_tool" clearable filterable>
                  <el-option
                    v-for="tool in toolCatalog.filter((item) => item.name !== node.tool_name)"
                    :key="tool.name"
                    :label="tool.name"
                    :value="tool.name"
                  />
                </el-select>
              </label>
            </div>
            <label class="full-field">
              <span>执行理由</span>
              <el-input v-model="node.reason" maxlength="1000" />
            </label>
            <label class="full-field">
              <span>完成条件</span>
              <el-input v-model="node.completion_condition" maxlength="1000" />
            </label>
            <label class="full-field">
              <span>工具参数 JSON</span>
              <el-input
                v-model="node.argumentsText"
                type="textarea"
                :rows="4"
                class="mono-input"
              />
            </label>
            <label class="full-field">
              <span>输出定义</span>
              <el-input
                v-model="node.output_definition.description"
                maxlength="1000"
                placeholder="说明该节点预期产生的结果，用于后续阶段数据流"
              />
            </label>
            <label
              v-if="node.failure_strategy === 'fallback_tool'"
              class="full-field"
            >
              <span>替代工具参数 JSON</span>
              <el-input
                v-model="node.fallback_arguments_text"
                type="textarea"
                :rows="3"
                class="mono-input"
              />
            </label>
            <div
              v-if="Object.keys(node.input_bindings).length"
              class="binding-preview"
            >
              <strong>数据流绑定</strong>
              <span
                v-for="(binding, argument) in node.input_bindings"
                :key="argument"
              >
                {{ argument }} ← {{ binding.source_node_id }}.{{ binding.output_key }}
              </span>
            </div>
          </article>
        </div>

        <el-alert
          v-if="planValidationError"
          :title="planValidationError"
          type="error"
          show-icon
          :closable="false"
        />
      </div>

      <template #footer>
        <el-button :disabled="savingPlan" @click="planEditorOpen = false">
          取消
        </el-button>
        <el-button
          type="primary"
          :loading="savingPlan"
          @click="savePlan"
        >
          保存新版本
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { Cpu } from "@element-plus/icons-vue";
import { ApiError } from "@/api/http";
import {
  cancelResearchAgentRun,
  confirmResearchAgentPlan,
  createResearchAgentPlan,
  createResearchAgentRun,
  fetchLatestResearchAgentRun,
  fetchResearchAgentTools,
  fetchResearchAgentReport,
  fetchResearchAgentRun,
  updateResearchAgentPlan,
  type ResearchAgentBudget,
  type ResearchAgentEvidence,
  type ResearchAgentPlanNode,
  type ResearchAgentReport,
  type ResearchAgentRun,
  type ResearchAgentToolCatalogItem,
} from "@/api/researchAgent";
import {
  createEditableNode,
  editableNodesToPlan,
  planNodesToEditable,
  type EditablePlanNode,
} from "@/utils/researchPlan";
import { formatDateTime } from "@/utils/format";

const defaultBudget: ResearchAgentBudget = {
  max_nodes: 8,
  max_tool_calls: 12,
  max_llm_calls: 4,
  max_arxiv_requests: 3,
  max_replans: 1,
  max_execution_seconds: 180,
};
const route = useRoute();
const router = useRouter();

interface PlanEditorForm {
  goal: string;
  topic: string;
  research_question: string;
  timeRange: string;
  analysisDimensionsText: string;
  budget: ResearchAgentBudget;
  nodes: EditablePlanNode[];
  changeReason: string;
}

const goal = ref("");
const run = ref<ResearchAgentRun | null>(null);
const report = ref<ResearchAgentReport | null>(null);
const creating = ref(false);
const planCreating = ref(false);
const cancelling = ref(false);
const savingPlan = ref(false);
const confirmingPlan = ref(false);
const planEditorOpen = ref(false);
const planValidationError = ref("");
const planForm = ref<PlanEditorForm | null>(null);
const toolCatalog = ref<ResearchAgentToolCatalogItem[]>([]);
const error = ref("");
let pollTimer: ReturnType<typeof setTimeout> | null = null;

const canCancel = computed(
  () => !!run.value && ["pending", "running"].includes(run.value.status),
);
const isDraft = computed(() => run.value?.status === "draft");
const validEvidence = computed(
  () => run.value?.evidence.filter((item) => item.is_valid) ?? [],
);
const completedNodeCount = computed(
  () => run.value?.nodes.filter((node) => node.status === "completed").length ?? 0,
);
const failedNodeCount = computed(
  () =>
    run.value?.nodes.filter(
      (node) => ["failed", "blocked"].includes(node.status),
    ).length ?? 0,
);
const nodeCompletionPercent = computed(() => {
  const total = run.value?.nodes.length ?? 0;
  return total ? Math.round((completedNodeCount.value / total) * 100) : 0;
});
const localEvidenceCount = computed(
  () =>
    run.value?.evidence.filter((item) => item.source_type === "local_full_text").length
    ?? 0,
);
const externalEvidenceCount = computed(
  () =>
    run.value?.evidence.filter(
      (item) => item.source_type === "external_arxiv_abstract",
    ).length ?? 0,
);
const invalidEvidenceCount = computed(
  () => run.value?.evidence.filter((item) => !item.is_valid).length ?? 0,
);

function statusLabel(status: string): string {
  return (
    {
      draft: "计划草案",
      pending: "待调度",
      running: "执行中",
      cancelling: "取消中",
      retry: "重试中",
      completed: "已完成",
      completed_with_warnings: "完成（有警告）",
      partial: "部分完成",
      failed: "失败",
      cancelled: "已取消",
      skipped: "已跳过",
      blocked: "依赖阻塞",
      interrupted: "执行中断",
    }[status] || status
  );
}

function planSourceLabel(source: string): string {
  if (source === "llm") return "LLM Planner";
  if (source === "fallback") return "Fallback Planner";
  if (source === "controlled_replan") return "运行时重规划";
  return source;
}

function planHistoryActionLabel(action: string | null): string {
  if (action === "generated") return "生成";
  if (action === "edited") return "用户编辑";
  return action || "版本";
}

function planNodeFor(nodeKey: string): ResearchAgentPlanNode | undefined {
  return run.value?.plan?.nodes.find((node) => node.id === nodeKey);
}

function statusType(status: string): "success" | "warning" | "danger" | "info" {
  if (status === "completed") return "success";
  if (status === "failed" || status === "blocked") return "danger";
  if (
    status === "running"
    || status === "retry"
    || status === "cancelling"
    || status === "partial"
    || status === "completed_with_warnings"
  ) return "warning";
  if (status === "cancelled" || status === "skipped" || status === "interrupted") return "info";
  return "info";
}

function toolStatusLabel(status: "success" | "empty_result" | "error"): string {
  if (status === "empty_result") return "正常空结果";
  if (status === "error") return "执行失败";
  return "返回成功";
}

function toolStatusType(
  status: "success" | "empty_result" | "error",
): "success" | "warning" | "danger" {
  if (status === "empty_result") return "warning";
  if (status === "error") return "danger";
  return "success";
}

function errorCategoryLabel(category: string): string {
  return (
    {
      validation_error: "参数或绑定校验失败",
      not_found: "资源不存在",
      empty_result: "正常空结果",
      timeout: "执行超时",
      rate_limited: "外部服务限流",
      external_service_error: "外部服务异常",
      internal_error: "内部执行异常",
    }[category] || category
  );
}

function strategyLabel(strategy: string): string {
  return (
    {
      retry_then_fail: "重试后终止",
      skip: "失败后跳过",
      fallback_tool: "替代工具",
      dependency_block: "依赖阻塞",
    }[strategy] || strategy
  );
}

function evidenceLabel(sourceType: string): string {
  return sourceType === "local_full_text"
    ? "本地全文"
    : "arXiv 外部摘要";
}

function evidenceLocator(item: ResearchAgentEvidence): string {
  if (item.source_type === "local_full_text") {
    return `paper ${item.paper_id} · p.${item.page_number} · chunk ${item.chunk_id}`;
  }
  return `arXiv:${item.arxiv_id} · 无全文页码`;
}

function reasonForCall(nodeId: string): string {
  return run.value?.nodes.find((node) => node.id === nodeId)?.reason || "—";
}

function stopPolling() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}


function syncRunRoute(runId: string) {
  if (route.query.run === runId) return;
  void router.replace({
    name: "research-agent",
    query: { ...route.query, run: runId },
  });
}


async function loadReport(runId: string) {
  try {
    report.value = await fetchResearchAgentReport(runId);
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "报告读取失败。";
  }
}

async function poll(runId: string) {
  stopPolling();
  try {
    run.value = await fetchResearchAgentRun(runId);
    if (["completed", "completed_with_warnings", "partial"].includes(run.value.status)) {
      if (run.value.report_available) await loadReport(runId);
      return;
    }
    if (["failed", "cancelled"].includes(run.value.status)) return;
    pollTimer = setTimeout(() => poll(runId), 1500);
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "任务状态读取失败。";
    pollTimer = setTimeout(() => poll(runId), 2500);
  }
}

async function createRunAutomatically() {
  if (goal.value.trim().length < 5 || creating.value) return;
  creating.value = true;
  error.value = "";
  report.value = null;
  stopPolling();
  try {
    run.value = await createResearchAgentRun(goal.value.trim(), defaultBudget);
    syncRunRoute(run.value.run_id);
    await poll(run.value.run_id);
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "研究任务创建失败。";
  } finally {
    creating.value = false;
  }
}

async function createPlan() {
  if (goal.value.trim().length < 5 || planCreating.value) return;
  planCreating.value = true;
  error.value = "";
  report.value = null;
  stopPolling();
  try {
    run.value = await createResearchAgentPlan(goal.value.trim(), defaultBudget);
    syncRunRoute(run.value.run_id);
    await loadToolCatalog();
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "研究计划生成失败。";
  } finally {
    planCreating.value = false;
  }
}

async function loadExistingRun(runId: string) {
  error.value = "";
  stopPolling();
  try {
    run.value = await fetchResearchAgentRun(runId);
    syncRunRoute(run.value.run_id);
    goal.value = run.value.goal;
    if (run.value.status === "completed") {
      await loadReport(runId);
    } else if (["pending", "running", "cancelling"].includes(run.value.status)) {
      await poll(runId);
    }
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "研究任务读取失败。";
  }
}

async function loadToolCatalog() {
  if (toolCatalog.value.length) return;
  try {
    toolCatalog.value = await fetchResearchAgentTools();
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "工具白名单读取失败。";
  }
}

function openPlanEditor() {
  if (!run.value?.plan) return;
  const plan = run.value.plan;
  planValidationError.value = "";
  planForm.value = {
    goal: run.value.goal,
    topic: plan.topic,
    research_question: plan.research_question,
    timeRange: plan.time_range || "",
    analysisDimensionsText: plan.analysis_dimensions.join("，"),
    budget: {
      max_nodes: run.value.budget.max_nodes,
      max_tool_calls: run.value.budget.max_tool_calls,
      max_llm_calls: run.value.budget.max_llm_calls,
      max_arxiv_requests: run.value.budget.max_arxiv_requests,
      max_replans: run.value.budget.max_replans,
      max_execution_seconds: run.value.budget.max_execution_seconds,
    },
    nodes: planNodesToEditable(plan),
    changeReason: "用户调整研究计划",
  };
  void loadToolCatalog();
  planEditorOpen.value = true;
}

function addPlanNode() {
  if (!planForm.value) return;
  const toolName = toolCatalog.value[0]?.name;
  if (!toolName) {
    planValidationError.value = "工具白名单尚未加载，暂时不能添加节点。";
    return;
  }
  planForm.value.nodes.push(
    createEditableNode(toolName, planForm.value.nodes.length + 1),
  );
}

function removePlanNode(index: number) {
  if (!planForm.value || planForm.value.nodes.length <= 1) return;
  const removedId = planForm.value.nodes[index]?.id;
  planForm.value.nodes.splice(index, 1);
  if (removedId) {
    planForm.value.nodes.forEach((node) => {
      node.depends_on = node.depends_on.filter((id) => id !== removedId);
    });
  }
}

async function savePlan() {
  if (!run.value || !planForm.value || savingPlan.value) return;
  planValidationError.value = "";
  savingPlan.value = true;
  try {
    const nodes = editableNodesToPlan(planForm.value.nodes);
    run.value = await updateResearchAgentPlan(run.value.run_id, {
      goal: planForm.value.goal.trim(),
      topic: planForm.value.topic.trim(),
      research_question: planForm.value.research_question.trim(),
      time_range: planForm.value.timeRange.trim() || null,
      analysis_dimensions: planForm.value.analysisDimensionsText
        .split(/[,，]/)
        .map((item) => item.trim())
        .filter(Boolean)
        .slice(0, 8),
      nodes,
      budget: { ...planForm.value.budget },
      change_reason: planForm.value.changeReason.trim() || "用户调整研究计划",
    });
    goal.value = run.value.goal;
    planEditorOpen.value = false;
  } catch (cause) {
    planValidationError.value = cause instanceof Error
      ? cause.message
      : "计划保存失败。";
  } finally {
    savingPlan.value = false;
  }
}

async function confirmPlan() {
  if (!run.value || run.value.status !== "draft" || confirmingPlan.value) return;
  confirmingPlan.value = true;
  error.value = "";
  try {
    run.value = await confirmResearchAgentPlan(run.value.run_id);
    await poll(run.value.run_id);
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "计划确认失败。";
  } finally {
    confirmingPlan.value = false;
  }
}

async function cancelRun() {
  if (!run.value || cancelling.value) return;
  cancelling.value = true;
  try {
    run.value = await cancelResearchAgentRun(run.value.run_id);
    stopPolling();
  } catch (cause) {
    error.value = cause instanceof ApiError ? cause.message : "取消任务失败。";
  } finally {
    cancelling.value = false;
  }
}

onMounted(() => {
  void loadToolCatalog();
  const requestedRunId = typeof route.query.run === "string"
    ? route.query.run
    : "";
  if (requestedRunId) {
    void loadExistingRun(requestedRunId);
    return;
  }
  void fetchLatestResearchAgentRun()
    .then((latest) => loadExistingRun(latest.run_id))
    .catch((cause) => {
      if (cause instanceof ApiError && cause.code === "RESEARCH_AGENT_NO_RUNS") {
        return;
      }
      error.value = cause instanceof ApiError
        ? cause.message
        : "最近研究任务读取失败。";
    });
});

onUnmounted(stopPolling);
</script>

<style scoped>
.agent-page {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.hero,
.goal-card,
.overview,
.panel,
.report {
  padding: 22px;
}

.hero,
.overview,
.panel-title {
  display: flex;
  justify-content: space-between;
  gap: 20px;
  align-items: flex-start;
}

.hero h2,
.panel-title h3,
.report h3 {
  margin: 0 0 8px;
}

.eyebrow {
  margin: 0 0 8px;
  color: var(--accent-2);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.14em;
}

.goal-card {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.goal-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.goal-heading h3 {
  margin: 3px 0;
}

.goal-heading p {
  margin: 0;
}

.agent-empty {
  display: flex;
  align-items: flex-start;
  gap: 16px;
  padding: 22px;
}

.empty-mark {
  display: grid;
  width: 44px;
  height: 44px;
  flex: 0 0 auto;
  place-items: center;
  color: var(--color-primary);
  background: var(--color-primary-soft);
  border-radius: var(--radius-lg);
}

.empty-mark svg {
  width: 22px;
}

.agent-empty h3 {
  margin: 0 0 5px;
}

.agent-empty p {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.7;
}

.capability-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}

.capability-list span {
  padding: 4px 8px;
  color: var(--color-text-secondary);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-pill);
  font-size: 10px;
}

.agent-pipeline {
  display: grid;
  padding: 0;
  margin: 16px 0 0;
  list-style: none;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.agent-pipeline li {
  display: flex;
  min-width: 0;
  padding: 10px;
  background: var(--color-bg-subtle);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  flex-direction: column;
  gap: 4px;
}

.agent-pipeline strong {
  color: var(--color-primary);
  font-family: var(--font-mono);
  font-size: 11px;
}

.agent-pipeline span {
  color: var(--color-text-tertiary);
  font-size: 10px;
  line-height: 1.45;
}

.goal-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
}

.goal-buttons,
.draft-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.overview {
  align-items: center;
}

.overview-main {
  flex: 1;
  min-width: 0;
}

.run-progress {
  max-width: 560px;
  margin-top: 12px;
}

.run-progress > div {
  display: flex;
  justify-content: space-between;
  margin-bottom: 5px;
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.run-progress strong {
  color: var(--color-text-primary);
  font-family: var(--font-mono);
}

.plan-estimate {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 10px;
}

.plan-estimate span {
  padding: 3px 8px;
  color: var(--color-text-tertiary);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-pill);
  font-size: 10px;
}

.worker-state {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  padding-top: 9px;
  margin-top: 8px;
  color: var(--color-text-tertiary);
  border-top: 1px solid var(--color-border-subtle);
  font-size: 10px;
}

.status-line,
.dimensions,
.node-head,
.node-meta,
.evidence-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.run-id {
  color: var(--muted);
  font-size: 12px;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(70px, 1fr));
  gap: 10px;
}

.metrics div {
  padding: 10px 12px;
  border-radius: 12px;
  background: var(--color-bg-subtle);
  text-align: center;
}

.metrics strong,
.metrics span {
  display: block;
}

.metrics span {
  color: var(--muted);
  font-size: 11px;
}

.grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px;
}

.plan-tags {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 5px;
}

.evidence-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  padding: 9px 10px;
  margin-top: 12px;
  color: var(--color-text-tertiary);
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
  font-size: 10px;
}

.node-list,
.evidence-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 16px;
}

.node,
.evidence {
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 13px;
  background: var(--color-bg-subtle);
}

.node.running {
  border-color: color-mix(in srgb, var(--color-warning) 38%, transparent);
  background: var(--color-warning-soft);
}

.node.completed {
  border-color: color-mix(in srgb, var(--color-success) 34%, transparent);
}

.node.failed,
.evidence.invalid {
  border-color: color-mix(in srgb, var(--color-danger) 34%, transparent);
  background: var(--color-danger-soft);
}

.node p,
.evidence p,
.report-section p,
.limitations p {
  line-height: 1.65;
}

.node-meta {
  color: var(--muted);
  font-size: 12px;
}

.node-details {
  margin-top: 10px;
  border-color: var(--color-border-subtle);
}

.node-details :deep(.el-collapse-item__header),
.node-details :deep(.el-collapse-item__wrap) {
  background: transparent;
  border-color: var(--color-border-subtle);
}

.node-details :deep(.el-collapse-item__header) {
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.node-details dl {
  margin: 0;
}

.node-details dl > div {
  display: grid;
  grid-template-columns: 100px minmax(0, 1fr);
  gap: 8px;
  padding: 7px 0;
  border-top: 1px solid var(--color-border-subtle);
}

.node-details dt {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.node-details dd {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  overflow-wrap: anywhere;
}

.binding-list {
  padding-left: 16px;
  margin: 0;
}

.binding-list li {
  margin: 3px 0;
}

.binding-list strong {
  color: var(--color-primary);
}

.node-json-output {
  max-height: 260px;
  padding: 9px;
  margin: 0;
  overflow: auto;
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-sm);
  font-size: 10px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.plan-history {
  padding: 4px 16px;
}

.execution-history {
  padding: 4px 16px;
}

.plan-history article {
  padding: 9px 0;
  border-top: 1px solid var(--color-border-subtle);
}

.execution-history article {
  padding: 10px 0;
  border-top: 1px solid var(--color-border-subtle);
}

.execution-history article:first-child {
  border-top: 0;
}

.execution-history article > div {
  display: flex;
  align-items: center;
  gap: 8px;
}

.execution-history article p {
  margin: 5px 0;
  color: var(--color-text-secondary);
  font-size: 11px;
}

.execution-history article small {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.plan-history article:first-child {
  border-top: 0;
}

.plan-history article > div {
  display: flex;
  align-items: center;
  gap: 8px;
}

.plan-history article p {
  margin: 5px 0 2px;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.plan-history article small {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.plan-editor {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.editor-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0 14px;
}

.budget-editor {
  padding: 14px;
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
}

.budget-editor h4,
.node-editor-heading h4 {
  margin: 0 0 10px;
}

.budget-editor > div {
  display: grid;
  grid-template-columns: repeat(3, minmax(110px, 1fr));
  gap: 10px;
}

.budget-editor label,
.node-editor-grid label,
.full-field {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 5px;
}

.budget-editor label span,
.node-editor-grid label span,
.full-field > span {
  color: var(--color-text-secondary);
  font-size: 11px;
}

.node-editor-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.node-editor-heading p {
  margin: 0;
}

.node-editor-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.node-editor-card {
  display: flex;
  padding: 14px;
  background: var(--color-bg-subtle);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  flex-direction: column;
  gap: 10px;
}

.node-editor-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.node-editor-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.critical-field :deep(.el-checkbox) {
  min-height: 32px;
}

.full-field {
  width: 100%;
}

.mono-input :deep(textarea) {
  font-family: var(--font-mono);
  font-size: 11px;
}

.binding-preview {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 9px 10px;
  background: var(--color-primary-soft);
  border-radius: var(--radius-sm);
  font-size: 10px;
}

.binding-preview span {
  color: var(--color-text-secondary);
  font-family: var(--font-mono);
}

.tool-option {
  display: flex;
  flex-direction: column;
  line-height: 1.35;
}

.tool-option small {
  overflow: hidden;
  color: var(--color-text-tertiary);
  font-size: 9px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.node-error {
  color: var(--danger);
}

.evidence-head {
  justify-content: space-between;
  font-size: 12px;
}

.summary {
  font-size: 12px;
  white-space: normal;
  word-break: break-word;
}

.report {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.summary-text {
  padding: 14px;
  border-left: 4px solid var(--accent-2);
  background: var(--color-accent-soft);
  line-height: 1.7;
}

.report-section {
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

.limitations {
  padding: 14px;
  border-radius: 12px;
  background: var(--color-bg-subtle);
}

@media (max-width: 980px) {
  .grid {
    grid-template-columns: 1fr;
  }

  .overview {
    align-items: stretch;
    flex-direction: column;
  }

  .budget-editor > div,
  .node-editor-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .hero,
  .goal-heading,
  .goal-actions,
  .overview,
  .panel-title {
    align-items: flex-start;
    flex-direction: column;
  }

  .metrics {
    width: 100%;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .plan-tags {
    justify-content: flex-start;
  }

  .agent-empty {
    flex-direction: column;
  }

  .agent-pipeline {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .goal-buttons,
  .draft-actions {
    width: 100%;
    justify-content: stretch;
    flex-direction: column;
  }

  .goal-buttons :deep(.el-button),
  .draft-actions :deep(.el-button) {
    width: 100%;
  }

  .editor-grid,
  .budget-editor > div,
  .node-editor-grid {
    grid-template-columns: 1fr;
  }

  .node-editor-heading {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
