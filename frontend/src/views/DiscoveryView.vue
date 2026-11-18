<template>
  <div class="discovery-page">
    <section class="page-card discovery-hero">
      <div>
        <p class="eyebrow">ARXIV DAILY DISCOVERY</p>
        <h2>每日论文推荐与文献发现</h2>
        <p class="muted">
          每日推荐来自 arXiv 官方公开 API；收藏前会再次回查官方元数据。
          推荐理由只说明排序依据，不把检索摘要包装成深度阅读结论。
        </p>
      </div>
      <div class="hero-tags">
        <el-tag type="success">arXiv 官方 API</el-tag>
        <el-tag type="warning">外部/本地来源分离</el-tag>
        <el-tag>现有导入任务</el-tag>
      </div>
    </section>

    <el-tabs v-model="activeTab" class="page-card discovery-tabs">
      <el-tab-pane label="每日论文推荐" name="daily">
        <div class="tab-body daily-body">
          <section class="daily-hero">
            <div class="daily-title">
              <div>
                <p class="eyebrow">TODAY · {{ dailyState?.date || "每日更新" }}</p>
                <h3>今日推荐</h3>
                <p class="muted">
                  首次进入当天页面或点击刷新时查询最新提交；结果保存在本地
                  SQLite，刷新页面后仍可查看收藏、已读和导入状态。
                </p>
              </div>
              <div class="daily-actions">
                <el-button
                  type="primary"
                  :loading="dailyRefreshing"
                  @click="startDailyRefresh(true)"
                >
                  手动刷新
                </el-button>
              </div>
            </div>

            <div class="interest-panel">
              <div class="interest-copy">
                <strong>研究兴趣设置</strong>
                <span>最多 8 个兴趣；推荐按关键词覆盖和提交时间排序。</span>
              </div>
              <el-select
                v-model="interestDraft"
                multiple
                filterable
                allow-create
                default-first-option
                :multiple-limit="8"
                placeholder="选择或输入研究兴趣"
                class="interest-select"
              >
                <el-option
                  v-for="interest in interestOptions"
                  :key="interest"
                  :label="interest"
                  :value="interest"
                />
              </el-select>
              <el-button
                plain
                :loading="interestsSaving"
                :disabled="!interestDraft.length"
                @click="saveInterests"
              >
                保存兴趣
              </el-button>
            </div>
          </section>

          <div
            v-if="dailyError || dailyState?.current?.status === 'failed'"
            class="inline-error"
          >
            <span>{{ dailyError || dailyState?.current?.error }}</span>
            <el-button link type="primary" @click="startDailyRefresh(true)">
              重新获取
            </el-button>
          </div>
          <el-alert
            v-if="dailyState?.current?.status === 'completed'
              && dailyState.current.error"
            :title="dailyState.current.error"
            type="warning"
            :closable="false"
            show-icon
          />

          <div
            v-if="dailyState?.current?.status === 'running'
              || dailyState?.current?.status === 'queued'"
            class="daily-loading"
            aria-live="polite"
          >
            <el-skeleton :rows="4" animated />
            <span>正在从 arXiv 官方 API 获取最新论文并按研究兴趣排序…</span>
          </div>

          <template v-else-if="dailyState?.current">
            <div class="daily-summary">
              <div>
                <strong>{{ dailyState.current.items.length }}</strong>
                <span>今日推荐</span>
              </div>
              <div>
                <strong>{{ dailyState.current.new_item_count }}</strong>
                <span>本次新增</span>
              </div>
              <div>
                <strong>{{ unreadDailyCount }}</strong>
                <span>未读</span>
              </div>
              <el-tag
                :type="dailyState.current.status === 'failed' ? 'danger' : 'success'"
                effect="plain"
              >
                {{ dailyState.current.status === "failed" ? "更新失败" : "arXiv 官方数据" }}
              </el-tag>
            </div>
            <p class="disclaimer">{{ dailyState.current.reason_disclaimer }}</p>
            <el-empty
              v-if="!dailyState.current.items.length"
              description="今天暂未找到符合研究兴趣的新论文；系统不会伪造推荐结果。"
            />
            <section v-else class="daily-grid">
              <article
                v-for="paper in dailyState.current.items"
                :key="paper.item_id"
                class="daily-card"
                :class="{ read: paper.is_read }"
              >
                <div class="daily-card-top">
                  <div class="tag-row">
                    <el-tag v-if="paper.is_read" size="small" type="info">已读</el-tag>
                    <el-tag size="small" type="warning">
                      arXiv:{{ paper.arxiv_id }}
                    </el-tag>
                    <el-tag v-if="paper.primary_category" size="small">
                      {{ paper.primary_category }}
                    </el-tag>
                    <el-tag
                      v-if="paper.favorite_id"
                      size="small"
                      type="success"
                    >
                      已收藏
                    </el-tag>
                  </div>
                  <span class="daily-score">匹配 {{ paper.score.toFixed(1) }}</span>
                </div>
                <h4>{{ paper.title }}</h4>
                <p class="authors">{{ paper.authors.join(", ") || "作者未知" }}</p>
                <div class="daily-meta">
                  <span>{{ formatArxivDate(paper.published) }}</span>
                  <span>相关度与新鲜度排序</span>
                </div>
                <p class="daily-abstract">{{ paper.summary }}</p>
                <div class="recommendation-reason">
                  <strong>推荐理由</strong>
                  <p>{{ paper.recommendation_reason }}</p>
                  <small>规则生成，不是论文内容结论</small>
                </div>
                <div class="daily-card-actions">
                  <el-button size="small" @click="openDailyDetails(paper)">
                    论文详情
                  </el-button>
                  <el-button
                    size="small"
                    plain
                    @click="toggleDailyRead(paper)"
                  >
                    {{ paper.is_read ? "标为未读" : "标为已读" }}
                  </el-button>
                  <el-button
                    v-if="!paper.favorite_id"
                    size="small"
                    :loading="dailyFavoritingId === paper.arxiv_id"
                    @click="favoriteDailyPaper(paper)"
                  >
                    收藏
                  </el-button>
                  <el-button
                    v-if="paper.local_paper_id"
                    size="small"
                    type="success"
                    @click="openLocalPaper(paper.local_paper_id)"
                  >
                    打开本地论文
                  </el-button>
                  <el-button
                    v-else
                    size="small"
                    type="primary"
                    :loading="dailyImportingId === paper.item_id"
                    :disabled="['queued', 'running'].includes(paper.favorite_import_status)"
                    @click="importDailyPaper(paper)"
                  >
                    {{
                      paper.favorite_import_status === "queued"
                        ? "等待导入"
                        : paper.favorite_import_status === "running"
                          ? "导入中"
                          : "导入文献库"
                    }}
                  </el-button>
                  <a :href="paper.abs_url" target="_blank" rel="noreferrer">
                    <el-button size="small" link type="primary">原文</el-button>
                  </a>
                  <a :href="paper.pdf_url" target="_blank" rel="noreferrer">
                    <el-button size="small" link type="primary">PDF</el-button>
                  </a>
                </div>
                <p v-if="paper.favorite_import_error" class="error-text">
                  导入失败：{{ paper.favorite_import_error }}
                </p>
              </article>
            </section>
          </template>

          <el-empty
            v-else
            description="当前没有今日推荐记录，点击手动刷新开始获取。"
          />
        </div>
      </el-tab-pane>

      <el-tab-pane label="外部搜索" name="search">
        <div class="tab-body">
          <section class="search-panel">
            <div class="search-panel-head">
              <div>
                <p class="eyebrow">ARXIV OFFICIAL SEARCH</p>
                <h3>发现外部研究文献</h3>
                <p class="muted">直接查询 arXiv 官方元数据，结果与本地馆藏分别标识。</p>
              </div>
              <el-tag effect="plain">来源：arXiv</el-tag>
            </div>
            <form class="search-form" @submit.prevent="runSearch(1)">
              <div class="primary-query">
                <el-input
                  v-model="searchQuery"
                  size="large"
                  clearable
                  placeholder="输入主题、方法、作者或论文关键词"
                  @keyup.enter="runSearch(1)"
                />
                <el-button
                  type="primary"
                  size="large"
                  native-type="submit"
                  :loading="searchLoading"
                  :disabled="searchQuery.trim().length < 2"
                >
                  搜索 arXiv
                </el-button>
              </div>
              <div class="search-options-line">
                <button
                  type="button"
                  class="advanced-toggle"
                  :aria-expanded="showAdvancedSearch"
                  @click="showAdvancedSearch = !showAdvancedSearch"
                >
                  {{ showAdvancedSearch ? "收起高级筛选" : "高级筛选" }}
                  <span>{{ searchFieldLabel }} · {{ searchSortLabel }}</span>
                </button>
                <span v-if="searchElapsedMs !== null" class="elapsed">
                  API 耗时 {{ searchElapsedMs }} ms
                </span>
              </div>
              <div v-if="showAdvancedSearch" class="advanced-filters">
                <el-form label-position="top">
                  <div class="advanced-grid">
                    <el-form-item label="检索字段">
                      <el-select v-model="searchField">
                        <el-option label="全部字段" value="all" />
                        <el-option label="标题" value="title" />
                        <el-option label="摘要" value="abstract" />
                        <el-option label="作者" value="author" />
                      </el-select>
                    </el-form-item>
                    <el-form-item label="排序依据">
                      <el-select v-model="searchSort">
                        <el-option label="相关度" value="relevance" />
                        <el-option label="最近更新" value="last_updated" />
                        <el-option label="提交日期" value="submitted" />
                      </el-select>
                    </el-form-item>
                    <el-form-item label="排序方向">
                      <el-select v-model="searchOrder">
                        <el-option label="降序" value="descending" />
                        <el-option label="升序" value="ascending" />
                      </el-select>
                    </el-form-item>
                  </div>
                </el-form>
              </div>
            </form>
            <div v-if="searchError" class="inline-error">
              <span>{{ searchError }}</span>
              <el-button link type="primary" @click="runSearch(1)">重试</el-button>
            </div>
          </section>

          <div v-if="searchLoading" class="result-skeleton-list" aria-live="polite">
            <div v-for="index in 3" :key="index" class="result-skeleton">
              <el-skeleton :rows="4" animated />
            </div>
          </div>

          <template v-else-if="searchResult">
            <div class="result-toolbar">
              <div>
                <strong>{{ searchResult.total_results }}</strong>
                <span class="muted"> 条官方结果 · 第 {{ searchResult.page }} 页</span>
              </div>
              <div class="result-status">
                <span v-if="searchElapsedMs !== null" class="muted">
                  实测 {{ searchElapsedMs }} ms
                </span>
                <el-tag type="success">官方 API</el-tag>
              </div>
            </div>
            <el-empty
              v-if="!searchResult.results.length"
              description="没有找到匹配论文；系统不会用虚构结果补足"
            />
            <section class="external-list">
              <article
                v-for="(paper, index) in searchResult.results"
                :key="paper.arxiv_id"
                class="external-card"
              >
                <span class="result-index">{{ index + 1 }}</span>
                <div class="card-main">
                  <div class="card-head">
                    <div class="result-meta">
                      <el-tag
                        :type="paper.in_local_library ? 'success' : 'info'"
                        size="small"
                      >
                        {{ externalResultLabel(paper) }}
                      </el-tag>
                      <el-tag size="small">arXiv:{{ paper.arxiv_id }}</el-tag>
                      <el-tag v-if="paper.primary_category" size="small" type="warning">
                        {{ paper.primary_category }}
                      </el-tag>
                      <span class="date muted">{{ formatArxivDate(paper.published) }}</span>
                    </div>
                    <h3>{{ paper.title }}</h3>
                    <p class="authors">{{ paper.authors.join(", ") || "作者未知" }}</p>
                  </div>
                  <p
                    class="summary"
                    :class="{ expanded: expandedAbstracts.includes(paper.arxiv_id) }"
                  >
                    {{ paper.summary }}
                  </p>
                  <button
                    type="button"
                    class="summary-toggle"
                    @click="toggleAbstract(paper.arxiv_id)"
                  >
                    {{ expandedAbstracts.includes(paper.arxiv_id) ? "收起摘要" : "展开摘要" }}
                  </button>
                  <div class="card-actions">
                    <a :href="paper.abs_url" target="_blank" rel="noreferrer">
                      <el-button size="small" plain>打开 arXiv</el-button>
                    </a>
                    <el-button
                      v-if="paper.local_paper_id"
                      size="small"
                      type="success"
                      @click="openLocalPaper(paper.local_paper_id)"
                    >
                      打开本地论文
                    </el-button>
                    <el-button
                      v-else
                      size="small"
                      type="primary"
                      :loading="favoritingId === paper.arxiv_id"
                      @click="favoritePaper(paper)"
                    >
                      {{ paper.favorite_id ? "已收藏" : "收藏并准备入库" }}
                    </el-button>
                  </div>
                </div>
              </article>
            </section>
            <el-pagination
              v-if="searchResult.total_results > searchPageSize"
              background
              layout="prev, pager, next"
              :page-size="searchPageSize"
              :current-page="searchResult.page"
              :total="Math.min(searchResult.total_results, searchPageSize * 100)"
              @current-change="runSearch"
            />
          </template>
        </div>
      </el-tab-pane>

      <el-tab-pane label="收藏与入库" name="favorites" lazy>
        <div class="tab-body">
          <div class="section-toolbar">
            <div>
              <p class="eyebrow">FAVORITES & IMPORT</p>
              <h3>收藏与入库队列</h3>
              <p class="muted">
                下载失败会保留收藏记录与具体原因；批量入库逐篇执行，单篇失败不会中断。
              </p>
            </div>
            <div class="section-actions">
              <el-button :loading="favoritesLoading" @click="loadFavorites">刷新状态</el-button>
              <el-button
                v-if="selectableFavorites.length"
                plain
                @click="toggleAllImportable"
              >
                {{ allImportableSelected ? "取消全选" : "全选可入库" }}
              </el-button>
              <el-button
                type="primary"
                :disabled="!batchSelectedIds.length || batchImporting"
                :loading="batchImporting"
                @click="batchImportFavorites"
              >
                批量入库（{{ batchSelectedIds.length }}）
              </el-button>
            </div>
          </div>
          <div v-if="batchImporting || batchTotal" class="batch-progress" aria-live="polite">
            <div>
              <strong>
                {{ batchImporting ? "正在创建入库任务" : "上次批量入库结果" }}
              </strong>
              <span>
                {{ batchProcessed }} / {{ batchTotal }} 篇已处理
                <template v-if="batchFailures.length">
                  · {{ batchFailures.length }} 篇失败
                </template>
              </span>
            </div>
            <el-progress :percentage="batchProgress" />
          </div>
          <div v-if="batchFailures.length" class="batch-failures">
            <strong>失败明细</strong>
            <p v-for="failure in batchFailures" :key="failure.title">
              {{ failure.title }}：{{ failure.message }}
            </p>
          </div>
          <div v-if="favoritesError" class="inline-error">
            <span>{{ favoritesError }}</span>
            <el-button link type="primary" @click="loadFavorites">重试</el-button>
          </div>
          <el-empty v-if="!favoritesLoading && !favorites.length" description="还没有收藏外部文献" />
          <section class="external-list">
            <article
              v-for="favorite in favorites"
              :key="favorite.favorite_id ?? favorite.arxiv_id"
              class="external-card"
            >
              <el-checkbox
                v-if="favorite.favorite_id"
                class="favorite-check"
                :model-value="batchSelectedIds.includes(favorite.favorite_id)"
                :disabled="!isFavoriteSelectable(favorite) || batchImporting"
                :aria-label="`选择 ${favorite.title}`"
                @change="toggleFavoriteSelection(favorite, Boolean($event))"
              />
              <div class="card-head">
                <div>
                  <div class="tag-row">
                    <el-tag size="small">arXiv:{{ favorite.arxiv_id }}</el-tag>
                    <el-tag
                      :type="favoriteImportStatusType(favorite.import_status)"
                      size="small"
                    >
                      {{ favoriteImportStatusLabel(favorite.import_status) }}
                    </el-tag>
                  </div>
                  <h3>{{ favorite.title }}</h3>
                </div>
                <span class="date muted">{{ formatArxivDate(favorite.published) }}</span>
              </div>
              <p class="authors">{{ favorite.authors.join(", ") || "作者未知" }}</p>
              <p class="summary">{{ favorite.summary }}</p>
              <p v-if="favorite.import_error" class="error-text">
                入库失败：{{ favorite.import_error }}
              </p>
              <div v-if="favorite.import_job" class="job-progress">
                <span>导入任务 {{ favorite.import_job.id.slice(0, 8) }}</span>
                <el-progress :percentage="favorite.import_job.progress" />
              </div>
              <div class="card-actions">
                <a :href="favorite.abs_url" target="_blank" rel="noreferrer">
                  <el-button size="small" plain>打开 arXiv</el-button>
                </a>
                <el-button
                  v-if="favorite.local_paper_id"
                  size="small"
                  type="success"
                  @click="openLocalPaper(favorite.local_paper_id)"
                >
                  打开本地论文
                </el-button>
                <el-button
                  v-else
                  size="small"
                  type="primary"
                  :loading="importingFavoriteId === favorite.favorite_id"
                  :disabled="['queued', 'running'].includes(favorite.import_status)"
                  @click="importFavorite(favorite)"
                >
                  下载 PDF 并入库
                </el-button>
              </div>
            </article>
          </section>
        </div>
      </el-tab-pane>

      <el-tab-pane label="关键词追踪" name="tracking" lazy>
        <div class="tab-body">
          <section class="tracking-form">
            <h3>保存 arXiv 追踪条件</h3>
            <el-form label-position="top">
              <div class="search-row">
                <el-form-item label="任务名称">
                  <el-input v-model="trackingName" placeholder="例如：RAG 最新论文" />
                </el-form-item>
                <el-form-item label="追踪关键词" class="query-field">
                  <el-input v-model="trackingQuery" placeholder="关键词或短语" />
                </el-form-item>
                <el-form-item label="检索字段">
                  <el-select v-model="trackingField">
                    <el-option label="全部字段" value="all" />
                    <el-option label="标题" value="title" />
                    <el-option label="摘要" value="abstract" />
                  </el-select>
                </el-form-item>
              </div>
              <el-button type="primary" :loading="trackingCreating" @click="createTracking">
                创建追踪任务
              </el-button>
            </el-form>
            <p v-if="trackingError" class="error-text">{{ trackingError }}</p>
          </section>

          <div class="section-toolbar">
            <div>
              <h3>追踪任务与历史</h3>
              <p class="muted">手动刷新；自动调度属于后续可选增强。</p>
            </div>
            <el-button :loading="trackingsLoading" @click="loadTrackings">刷新列表</el-button>
          </div>
          <el-empty v-if="!trackingsLoading && !trackings.length" description="还没有追踪任务" />
          <section class="tracking-list">
            <article
              v-for="tracking in trackings"
              :key="tracking.tracking_id"
              class="tracking-card"
            >
              <div class="card-head">
                <div>
                  <h3>{{ tracking.name }}</h3>
                  <p class="muted">
                    {{ tracking.query }} · {{ tracking.field }} · {{ trackingPageSize }} 条/次
                  </p>
                </div>
                <el-button
                  size="small"
                  type="primary"
                  :loading="refreshingTrackingId === tracking.tracking_id"
                  @click="refreshTracking(tracking)"
                >
                  手动刷新
                </el-button>
              </div>
              <div class="tracking-stats">
                <span>最近状态：{{ tracking.latest_run?.status || "尚未运行" }}</span>
                <span>本次新增：{{ tracking.new_count_latest }}</span>
                <span>历史运行：{{ tracking.run_count }}</span>
                <span>
                  最近刷新：{{ tracking.last_refreshed_at ? formatArxivDate(tracking.last_refreshed_at) : "从未" }}
                </span>
              </div>
              <p v-if="tracking.latest_run?.error_message" class="error-text">
                {{ tracking.latest_run.error_message }}
              </p>
              <div v-if="tracking.latest_run?.results?.length" class="tracking-results">
                <article
                  v-for="result in tracking.latest_run.results"
                  :key="`${tracking.tracking_id}-${result.arxiv_id}`"
                  class="mini-result"
                >
                  <div class="tag-row">
                    <el-tag v-if="result.is_new" type="success" size="small">本次新增</el-tag>
                    <el-tag size="small">arXiv:{{ result.arxiv_id }}</el-tag>
                  </div>
                  <strong>{{ result.title }}</strong>
                  <a :href="result.abs_url" target="_blank" rel="noreferrer">查看官方页面</a>
                </article>
              </div>
              <el-table
                v-if="tracking.history.length"
                :data="tracking.history"
                size="small"
                class="history-table"
              >
                <el-table-column prop="run_id" label="运行" width="70" />
                <el-table-column prop="status" label="状态" width="100" />
                <el-table-column prop="total_results" label="返回" width="80" />
                <el-table-column prop="new_result_count" label="新增" width="80" />
                <el-table-column prop="started_at" label="开始时间" min-width="180" />
              </el-table>
            </article>
          </section>
        </div>
      </el-tab-pane>

      <el-tab-pane label="相关推荐" name="recommendations" lazy>
        <div class="tab-body">
          <section class="recommendation-head">
            <div>
              <h3>基于收藏与追踪关键词的推荐</h3>
              <p class="muted">
                外部推荐来自 arXiv；本地推荐复用现有 RAG，两者分别标识。
              </p>
            </div>
            <el-button
              type="primary"
              :loading="recommendationLoading"
              @click="loadRecommendations"
            >
              生成推荐
            </el-button>
          </section>
          <p v-if="recommendationError" class="error-text">{{ recommendationError }}</p>
          <template v-if="recommendations">
            <p class="disclaimer">{{ recommendations.disclaimer }}</p>
            <div class="seed-box">
              <strong>推荐种子</strong>
              <span>{{ recommendations.seed.query }}</span>
              <el-tag size="small">
                {{ recommendations.seed.source === "favorite_paper" ? "收藏论文" : "追踪关键词" }}
              </el-tag>
            </div>

            <section>
              <div class="source-title">
                <h3>arXiv 外部推荐</h3>
                <el-tag type="warning">外部结果</el-tag>
              </div>
              <el-empty
                v-if="!recommendations.external_results.length"
                description="arXiv 暂无相关结果"
              />
              <div class="recommendation-grid">
                <article
                  v-for="result in recommendations.external_results"
                  :key="result.arxiv_id"
                  class="recommendation-card"
                >
                  <div class="tag-row">
                    <el-tag size="small" type="warning">arXiv:{{ result.arxiv_id }}</el-tag>
                    <el-tag v-if="result.in_local_library" size="small" type="success">
                      本地已有
                    </el-tag>
                  </div>
                  <h4>{{ result.title }}</h4>
                  <p>{{ result.summary }}</p>
                  <a :href="result.abs_url" target="_blank" rel="noreferrer">
                    打开官方页面
                  </a>
                </article>
              </div>
            </section>

            <section>
              <div class="source-title">
                <h3>本地 RAG 推荐</h3>
                <el-tag type="success">本地论文</el-tag>
              </div>
              <el-alert
                v-if="recommendations.local_rag_status === 'unavailable'"
                type="warning"
                :closable="false"
                title="本地 RAG 暂时不可用，本次仅展示真实外部结果"
              />
              <el-empty
                v-else-if="!recommendations.local_recommendations.length"
                description="本地论文库暂无相关证据"
              />
              <div class="recommendation-grid">
                <article
                  v-for="paper in recommendations.local_recommendations"
                  :key="paper.paper_id"
                  class="recommendation-card local"
                >
                  <div class="tag-row">
                    <el-tag size="small" type="success">本地已有论文</el-tag>
                    <el-tag size="small">Page {{ paper.page_number }}</el-tag>
                  </div>
                  <h4>{{ paper.title }}</h4>
                  <p>{{ paper.evidence }}</p>
                  <el-button link type="primary" @click="openLocalPaper(paper.paper_id, paper.page_number)">
                    打开本地证据
                  </el-button>
                </article>
              </div>
            </section>
          </template>
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-dialog
      v-model="dailyDetailsOpen"
      :title="selectedDailyPaper?.title || '论文详情'"
      width="min(760px, calc(100vw - 32px))"
    >
      <div v-if="selectedDailyPaper" class="daily-detail">
        <div class="tag-row">
          <el-tag type="warning">arXiv:{{ selectedDailyPaper.arxiv_id }}</el-tag>
          <el-tag v-if="selectedDailyPaper.primary_category">
            {{ selectedDailyPaper.primary_category }}
          </el-tag>
          <el-tag v-if="selectedDailyPaper.is_read" type="info">已读</el-tag>
        </div>
        <dl>
          <div>
            <dt>作者</dt>
            <dd>{{ selectedDailyPaper.authors.join(", ") || "作者未知" }}</dd>
          </div>
          <div>
            <dt>提交时间</dt>
            <dd>{{ formatArxivDate(selectedDailyPaper.published) }}</dd>
          </div>
          <div>
            <dt>arXiv 分类</dt>
            <dd>
              {{ selectedDailyPaper.categories?.join(", ")
                || selectedDailyPaper.primary_category
                || "未提供" }}
            </dd>
          </div>
        </dl>
        <section>
          <h4>arXiv 原始摘要</h4>
          <p>{{ selectedDailyPaper.summary }}</p>
        </section>
        <section class="recommendation-reason">
          <strong>规则生成的推荐理由</strong>
          <p>{{ selectedDailyPaper.recommendation_reason }}</p>
          <small>
            匹配兴趣：{{ selectedDailyPaper.matched_interests.join("、") }}。
            该理由不构成对论文方法、实验结果或结论的真实性判断。
          </small>
        </section>
        <div class="card-actions">
          <a :href="selectedDailyPaper.abs_url" target="_blank" rel="noreferrer">
            <el-button type="primary" plain>打开 arXiv 原文页</el-button>
          </a>
          <a :href="selectedDailyPaper.pdf_url" target="_blank" rel="noreferrer">
            <el-button plain>打开 PDF</el-button>
          </a>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import {
  createDiscoveryTracking,
  favoriteExternal,
  fetchDailyRecommendationState,
  fetchDiscoveryRecommendations,
  fetchDiscoveryTrackings,
  fetchExternalFavorites,
  importFavoritePdf,
  markDailyRecommendationRead,
  patchFavoriteState,
  refreshDailyRecommendations,
  refreshDiscoveryTracking,
  saveDiscoveryInterests,
  searchExternalLiterature,
} from "@/api/discovery";
import { ApiError } from "@/api/http";
import type {
  DailyRecommendationPaper,
  DailyRecommendationState,
  DiscoveryField,
  DiscoveryRecommendations,
  DiscoverySortBy,
  DiscoverySortOrder,
  ExternalFavorite,
  ExternalPaperResult,
  ExternalSearchResult,
  TrackingSearch,
} from "@/types/discovery";
import {
  buildTrackingPayload,
  externalResultLabel,
  favoriteImportStatusLabel,
  favoriteImportStatusType,
  formatArxivDate,
  hasActiveFavoriteImport,
} from "@/utils/discovery";
import { createTaskPoller } from "@/utils/taskPoller";

const router = useRouter();
const activeTab = ref("daily");
const interestOptions = [
  "人工智能",
  "大语言模型",
  "RAG",
  "AI Agent",
  "计算机网络",
  "知识图谱",
  "文献智能分析",
];
const dailyState = ref<DailyRecommendationState | null>(null);
const interestDraft = ref<string[]>([]);
const dailyRefreshing = ref(false);
const interestsSaving = ref(false);
const dailyError = ref("");
const dailyFavoritingId = ref("");
const dailyImportingId = ref<number | null>(null);
const dailyDetailsOpen = ref(false);
const selectedDailyPaper = ref<DailyRecommendationPaper | null>(null);
let dailyPollTimer: ReturnType<typeof setTimeout> | null = null;
const unreadDailyCount = computed(
  () => dailyState.value?.current?.items.filter((paper) => !paper.is_read).length ?? 0,
);
const searchQuery = ref("");
const searchField = ref<DiscoveryField>("all");
const searchSort = ref<DiscoverySortBy>("relevance");
const searchOrder = ref<DiscoverySortOrder>("descending");
const searchPageSize = ref(10);
const searchResult = ref<ExternalSearchResult | null>(null);
const searchLoading = ref(false);
const searchError = ref("");
const favoritingId = ref("");
const searchElapsedMs = ref<number | null>(null);
const showAdvancedSearch = ref(false);
const expandedAbstracts = ref<string[]>([]);
const searchFieldLabel = computed(
  () =>
    ({
      all: "全部字段",
      title: "标题",
      abstract: "摘要",
      author: "作者",
    })[searchField.value],
);
const searchSortLabel = computed(
  () =>
    ({
      relevance: "相关度",
      last_updated: "最近更新",
      submitted: "提交日期",
    })[searchSort.value],
);

const favorites = ref<ExternalFavorite[]>([]);
const favoritesLoading = ref(false);
const favoritesError = ref("");
const importingFavoriteId = ref<number | null>(null);
const batchSelectedIds = ref<number[]>([]);
const batchImporting = ref(false);
const batchProcessed = ref(0);
const batchTotal = ref(0);
const batchFailures = ref<Array<{ title: string; message: string }>>([]);
const favoritesPoller = createTaskPoller({
  fetchValue: fetchExternalFavorites,
  isActive: hasActiveFavoriteImport,
  onValue: (loadedFavorites) => {
    favorites.value = loadedFavorites;
    favoritesError.value = "";
    if (!hasActiveFavoriteImport(loadedFavorites)) favoritesPoller.stop();
  },
  onError: (error) => {
    if (activeTab.value !== "favorites") return;
    favoritesError.value = error instanceof ApiError
      ? error.message
      : "加载收藏失败";
  },
  intervalMs: 1500,
});

const trackingName = ref("");
const trackingQuery = ref("");
const trackingField = ref<DiscoveryField>("all");
const trackingPageSize = ref(20);
const trackings = ref<TrackingSearch[]>([]);
const trackingsLoading = ref(false);
const trackingCreating = ref(false);
const trackingError = ref("");
const refreshingTrackingId = ref<number | null>(null);

const recommendations = ref<DiscoveryRecommendations | null>(null);
const recommendationLoading = ref(false);
const recommendationError = ref("");

const selectableFavorites = computed(() =>
  favorites.value.filter(isFavoriteSelectable),
);
const allImportableSelected = computed(() =>
  selectableFavorites.value.length > 0
  && selectableFavorites.value.every((favorite) =>
    batchSelectedIds.value.includes(favorite.favorite_id as number),
  ),
);
const batchProgress = computed(() =>
  batchTotal.value
    ? Math.round((batchProcessed.value / batchTotal.value) * 100)
    : 0,
);

function stopDailyPolling() {
  if (dailyPollTimer) clearTimeout(dailyPollTimer);
  dailyPollTimer = null;
}

function scheduleDailyPoll(reason: "refresh" | "import") {
  stopDailyPolling();
  dailyPollTimer = setTimeout(() => {
    void pollDailyState(reason);
  }, reason === "refresh" ? 1500 : 2500);
}

function applyDailyState(state: DailyRecommendationState) {
  dailyState.value = state;
  if (!interestDraft.value.length) {
    interestDraft.value = [...state.profile.interests];
  }
}

async function pollDailyState(reason: "refresh" | "import") {
  try {
    const state = await fetchDailyRecommendationState();
    applyDailyState(state);
    dailyError.value = "";
    const batchActive = ["queued", "running"].includes(
      state.current?.status || "",
    );
    const importActive = state.current?.items.some((paper) =>
      ["queued", "running"].includes(paper.favorite_import_status),
    ) ?? false;
    dailyRefreshing.value = batchActive;
    if (batchActive) scheduleDailyPoll("refresh");
    else if (importActive) scheduleDailyPoll("import");
    else stopDailyPolling();
  } catch (error) {
    dailyError.value = error instanceof ApiError
      ? error.message
      : "每日推荐状态读取失败";
    dailyRefreshing.value = false;
    scheduleDailyPoll(reason);
  }
}

async function loadDailyState() {
  try {
    const state = await fetchDailyRecommendationState();
    applyDailyState(state);
    dailyError.value = "";
    if (!state.current) {
      await startDailyRefresh(false);
    } else if (["queued", "running"].includes(state.current.status)) {
      dailyRefreshing.value = true;
      scheduleDailyPoll("refresh");
    }
  } catch (error) {
    dailyError.value = error instanceof ApiError
      ? error.message
      : "每日推荐加载失败";
  }
}

async function startDailyRefresh(force: boolean) {
  if (dailyRefreshing.value) return;
  dailyRefreshing.value = true;
  dailyError.value = "";
  try {
    const batch = await refreshDailyRecommendations(force);
    if (dailyState.value && batch) {
      dailyState.value.current = batch;
    } else {
      await pollDailyState("refresh");
    }
    if (["queued", "running"].includes(batch?.status || "")) {
      scheduleDailyPoll("refresh");
    } else {
      dailyRefreshing.value = false;
    }
  } catch (error) {
    dailyRefreshing.value = false;
    dailyError.value = error instanceof ApiError
      ? error.message
      : "arXiv 每日推荐刷新失败";
  }
}

async function saveInterests() {
  if (!interestDraft.value.length || interestsSaving.value) return;
  interestsSaving.value = true;
  try {
    const profile = await saveDiscoveryInterests(interestDraft.value);
    if (dailyState.value) dailyState.value.profile = profile;
    interestDraft.value = [...profile.interests];
    ElMessage.success("研究兴趣已保存；下次刷新将按新兴趣生成推荐");
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "研究兴趣保存失败");
  } finally {
    interestsSaving.value = false;
  }
}

function replaceDailyPaper(updated: DailyRecommendationPaper) {
  if (dailyState.value?.current) {
    dailyState.value.current.items = dailyState.value.current.items.map((paper) =>
      paper.item_id === updated.item_id ? updated : paper,
    );
  }
  if (selectedDailyPaper.value?.item_id === updated.item_id) {
    selectedDailyPaper.value = updated;
  }
}

async function toggleDailyRead(paper: DailyRecommendationPaper) {
  try {
    const updated = await markDailyRecommendationRead(
      paper.item_id,
      !paper.is_read,
    );
    replaceDailyPaper(updated);
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "已读状态保存失败");
  }
}

async function ensureDailyFavorite(paper: DailyRecommendationPaper) {
  if (paper.favorite_id) return paper.favorite_id;
  const favorite = await favoriteExternal(paper.arxiv_id);
  paper.favorite_id = favorite.favorite_id;
  return favorite.favorite_id;
}

async function favoriteDailyPaper(paper: DailyRecommendationPaper) {
  dailyFavoritingId.value = paper.arxiv_id;
  try {
    await ensureDailyFavorite(paper);
    replaceDailyPaper({ ...paper });
    ElMessage.success("已收藏，原始元数据已由服务端回查");
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "收藏失败");
  } finally {
    dailyFavoritingId.value = "";
  }
}

async function importDailyPaper(paper: DailyRecommendationPaper) {
  if (paper.local_paper_id) {
    openLocalPaper(paper.local_paper_id);
    return;
  }
  dailyImportingId.value = paper.item_id;
  try {
    const favoriteId = await ensureDailyFavorite(paper);
    if (!favoriteId) throw new Error("收藏记录未返回有效 ID");
    const result = await importFavoritePdf(favoriteId);
    paper.favorite_import_status = result.favorite.import_status;
    paper.favorite_import_error = result.favorite.import_error;
    paper.local_paper_id = result.favorite.local_paper_id;
    replaceDailyPaper({ ...paper });
    ElMessage.success(
      result.status === "already_in_library"
        ? "本地已有相同论文"
        : "已进入现有 PDF 下载与导入任务",
    );
    await pollDailyState("import");
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "论文导入失败");
  } finally {
    dailyImportingId.value = null;
  }
}

function openDailyDetails(paper: DailyRecommendationPaper) {
  selectedDailyPaper.value = paper;
  dailyDetailsOpen.value = true;
}

async function runSearch(page: number) {
  const query = searchQuery.value.trim();
  if (query.length < 2) return;
  searchLoading.value = true;
  searchError.value = "";
  searchElapsedMs.value = null;
  const startedAt = performance.now();
  try {
    searchResult.value = await searchExternalLiterature({
      query,
      field: searchField.value,
      page,
      pageSize: searchPageSize.value,
      sortBy: searchSort.value,
      sortOrder: searchOrder.value,
    });
    expandedAbstracts.value = [];
  } catch (error) {
    searchError.value = error instanceof ApiError ? error.message : "arXiv 搜索失败";
  } finally {
    searchElapsedMs.value = Math.round(performance.now() - startedAt);
    searchLoading.value = false;
  }
}

async function favoritePaper(paper: ExternalPaperResult) {
  favoritingId.value = paper.arxiv_id;
  try {
    const favorite = await favoriteExternal(paper.arxiv_id);
    if (searchResult.value) {
      searchResult.value.results = searchResult.value.results.map((item) =>
        item.arxiv_id === paper.arxiv_id
          ? patchFavoriteState(item, favorite)
          : item,
      );
    }
    ElMessage.success("已收藏；原始 arXiv 元数据已由服务端复核");
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "收藏失败");
  } finally {
    favoritingId.value = "";
  }
}

async function loadFavorites() {
  favoritesLoading.value = true;
  favoritesError.value = "";
  try {
    const loadedFavorites = await fetchExternalFavorites();
    favorites.value = loadedFavorites;
    const availableIds = new Set(
      loadedFavorites
        .filter(isFavoriteSelectable)
        .map((favorite) => favorite.favorite_id),
    );
    batchSelectedIds.value = batchSelectedIds.value.filter((id) =>
      availableIds.has(id),
    );
    if (
      activeTab.value === "favorites"
      && hasActiveFavoriteImport(loadedFavorites)
    ) {
      favoritesPoller.start();
    } else {
      favoritesPoller.stop();
    }
  } catch (error) {
    favoritesError.value = error instanceof ApiError ? error.message : "加载收藏失败";
  } finally {
    favoritesLoading.value = false;
  }
}

function isFavoriteSelectable(favorite: ExternalFavorite): boolean {
  if (!favorite.favorite_id || favorite.local_paper_id) return false;
  if (["queued", "running"].includes(favorite.import_status)) return false;
  return !(
    favorite.import_job
    && ["queued", "running"].includes(favorite.import_job.status)
  );
}

function toggleFavoriteSelection(favorite: ExternalFavorite, checked: boolean) {
  if (!favorite.favorite_id) return;
  const current = new Set(batchSelectedIds.value);
  if (checked) current.add(favorite.favorite_id);
  else current.delete(favorite.favorite_id);
  batchSelectedIds.value = [...current];
}

function toggleAllImportable() {
  batchSelectedIds.value = allImportableSelected.value
    ? []
    : selectableFavorites.value.map((favorite) => favorite.favorite_id as number);
}

async function batchImportFavorites() {
  const targets = favorites.value.filter((favorite) =>
    favorite.favorite_id
    && batchSelectedIds.value.includes(favorite.favorite_id),
  );
  if (!targets.length || batchImporting.value) return;
  try {
    await ElMessageBox.confirm(
      `将为 ${targets.length} 篇收藏顺序创建 PDF 下载与入库任务。单篇失败不会阻止后续文献。`,
      "批量下载并入库",
      { type: "warning", confirmButtonText: "开始入库", cancelButtonText: "取消" },
    );
  } catch {
    return;
  }

  batchImporting.value = true;
  batchProcessed.value = 0;
  batchTotal.value = targets.length;
  batchFailures.value = [];
  for (const favorite of targets) {
    if (!favorite.favorite_id) continue;
    try {
      const result = await importFavoritePdf(favorite.favorite_id);
      favorite.import_status = result.favorite.import_status;
      favorite.import_job = result.import_job;
      favorite.local_paper_id = result.favorite.local_paper_id;
      favorite.import_error = result.favorite.import_error;
    } catch (error) {
      batchFailures.value.push({
        title: favorite.title,
        message: error instanceof ApiError ? error.message : "入库失败",
      });
    } finally {
      batchProcessed.value += 1;
    }
  }
  batchSelectedIds.value = [];
  batchImporting.value = false;
  await loadFavorites();
  if (batchFailures.value.length) {
    ElMessage.warning(
      `批量入库完成，${batchFailures.value.length} 篇失败，失败原因已保留。`,
    );
  } else {
    ElMessage.success(`已为 ${targets.length} 篇文献创建入库任务。`);
  }
}

function toggleAbstract(arxivId: string) {
  expandedAbstracts.value = expandedAbstracts.value.includes(arxivId)
    ? expandedAbstracts.value.filter((id) => id !== arxivId)
    : [...expandedAbstracts.value, arxivId];
}

async function importFavorite(favorite: ExternalFavorite) {
  if (!favorite.favorite_id) return;
  importingFavoriteId.value = favorite.favorite_id;
  try {
    const result = await importFavoritePdf(favorite.favorite_id);
    favorite.import_status = result.favorite.import_status;
    favorite.import_job = result.import_job;
    favorite.local_paper_id = result.favorite.local_paper_id;
    favorite.import_error = result.favorite.import_error;
    ElMessage.success(
      result.status === "already_in_library"
        ? "检测到本地已有相同 arXiv 论文或 PDF SHA"
        : "PDF 已进入现有导入任务队列",
    );
    await loadFavorites();
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "PDF 入库失败");
    await loadFavorites();
  } finally {
    importingFavoriteId.value = null;
  }
}

async function createTracking() {
  trackingCreating.value = true;
  trackingError.value = "";
  try {
    const payload = buildTrackingPayload({
      name: trackingName.value,
      query: trackingQuery.value,
      field: trackingField.value,
      sortBy: "submitted",
      sortOrder: "descending",
      pageSize: trackingPageSize.value,
    });
    await createDiscoveryTracking(payload);
    trackingName.value = "";
    trackingQuery.value = "";
    await loadTrackings();
    ElMessage.success("追踪任务已保存");
  } catch (error) {
    trackingError.value = error instanceof ApiError
      ? error.message
      : error instanceof Error
        ? error.message
        : "创建追踪失败";
  } finally {
    trackingCreating.value = false;
  }
}

async function loadTrackings() {
  trackingsLoading.value = true;
  try {
    trackings.value = await fetchDiscoveryTrackings();
  } catch (error) {
    trackingError.value = error instanceof ApiError ? error.message : "加载追踪失败";
  } finally {
    trackingsLoading.value = false;
  }
}

async function refreshTracking(tracking: TrackingSearch) {
  refreshingTrackingId.value = tracking.tracking_id;
  try {
    const updated = await refreshDiscoveryTracking(tracking.tracking_id);
    trackings.value = trackings.value.map((item) =>
      item.tracking_id === updated.tracking_id ? updated : item,
    );
    ElMessage.success(`刷新完成，本次新增 ${updated.new_count_latest} 篇`);
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : "追踪刷新失败");
    await loadTrackings();
  } finally {
    refreshingTrackingId.value = null;
  }
}

async function loadRecommendations() {
  recommendationLoading.value = true;
  recommendationError.value = "";
  try {
    recommendations.value = await fetchDiscoveryRecommendations();
  } catch (error) {
    recommendationError.value = error instanceof ApiError
      ? error.message
      : "推荐生成失败";
  } finally {
    recommendationLoading.value = false;
  }
}

function openLocalPaper(paperId: number, page = 1) {
  router.push({
    name: "paper-detail",
    params: { id: paperId },
    query: { page: String(page) },
  });
}

watch(activeTab, (tab) => {
  if (tab === "favorites") void loadFavorites();
  else favoritesPoller.stop();
  if (tab === "tracking") void loadTrackings();
});

onMounted(() => {
  void loadDailyState();
  void loadTrackings();
});

onUnmounted(() => {
  stopDailyPolling();
  favoritesPoller.stop();
});
</script>

<style scoped>
.discovery-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1380px;
  margin: 0 auto;
}

.discovery-hero,
.card-head,
.search-actions,
.result-toolbar,
.section-toolbar,
.recommendation-head,
.source-title,
.seed-box {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.discovery-hero {
  padding: 22px 24px;
}

.discovery-hero h2 {
  margin: 4px 0 6px;
}

.eyebrow {
  margin: 0;
  color: var(--accent-2);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.12em;
}

.hero-tags,
.tag-row,
.card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.discovery-tabs {
  padding: 8px 18px 22px;
}

.tab-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.daily-body {
  gap: 14px;
}

.daily-hero {
  display: flex;
  padding: 20px;
  background:
    linear-gradient(
      135deg,
      color-mix(in srgb, var(--color-primary-soft) 72%, transparent),
      color-mix(in srgb, var(--color-accent-soft) 52%, transparent)
    );
  border: 1px solid var(--color-primary-border);
  border-radius: 14px;
  flex-direction: column;
  gap: 16px;
}

.daily-title {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 18px;
}

.daily-title > div:first-child {
  min-width: 0;
}

.daily-title h3 {
  margin: 4px 0 6px;
  font-size: 20px;
}

.daily-title p {
  margin: 0;
}

.daily-actions {
  flex: 0 0 auto;
}

.interest-panel {
  display: grid;
  min-width: 0;
  align-items: center;
  padding: 12px;
  background: color-mix(in srgb, var(--color-surface) 78%, transparent);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
  grid-template-columns: minmax(180px, 0.7fr) minmax(260px, 1.5fr) auto;
  gap: 12px;
}

.interest-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
}

.interest-copy span {
  color: var(--color-text-tertiary);
  font-size: 11px;
}

.interest-select {
  min-width: 0;
  width: 100%;
}

.daily-loading {
  display: grid;
  padding: 20px;
  background: var(--color-surface);
  border: 1px solid var(--line);
  border-radius: 14px;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 18px;
}

.daily-loading span {
  max-width: 240px;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.daily-summary {
  display: flex;
  align-items: stretch;
  flex-wrap: wrap;
  gap: 8px;
}

.daily-summary > div {
  display: flex;
  min-width: 110px;
  padding: 10px 14px;
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
  flex-direction: column;
}

.daily-summary strong {
  font-family: var(--font-mono);
  font-size: 20px;
}

.daily-summary span {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.daily-summary > :deep(.el-tag) {
  align-self: center;
  margin-left: auto;
}

.daily-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.daily-card {
  display: flex;
  min-width: 0;
  padding: 18px;
  background: var(--color-surface);
  border: 1px solid var(--line);
  border-top: 3px solid var(--accent-2);
  border-radius: 14px;
  box-shadow: var(--shadow-xs);
  flex-direction: column;
  gap: 9px;
}

.daily-card.read {
  border-top-color: var(--color-border-strong);
  opacity: 0.82;
}

.daily-card-top {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.daily-score {
  flex: 0 0 auto;
  color: var(--color-text-tertiary);
  font-family: var(--font-mono);
  font-size: 10px;
}

.daily-card h4 {
  margin: 0;
  font-size: 16px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.daily-meta {
  display: flex;
  flex-wrap: wrap;
  color: var(--color-text-tertiary);
  font-size: 11px;
  gap: 6px 16px;
}

.daily-abstract {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.recommendation-reason {
  padding: 10px 12px;
  color: var(--color-warning);
  background: var(--color-warning-soft);
  border-radius: var(--radius-md);
}

.recommendation-reason p {
  margin: 5px 0;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.recommendation-reason small {
  color: var(--color-text-tertiary);
}

.daily-card-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: auto;
}

.daily-detail {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.daily-detail dl {
  display: grid;
  margin: 0;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.daily-detail dl > div {
  min-width: 0;
  padding: 10px;
  background: var(--color-bg-subtle);
  border-radius: var(--radius-md);
}

.daily-detail dt {
  color: var(--color-text-tertiary);
  font-size: 10px;
}

.daily-detail dd {
  margin: 4px 0 0;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

.daily-detail section p {
  line-height: 1.75;
  white-space: pre-wrap;
}

.search-panel,
.tracking-form {
  padding: 20px;
  background: var(--color-bg-subtle);
  border: 1px solid var(--line);
  border-radius: 14px;
}

.search-panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.search-panel-head h3 {
  margin: 4px 0;
  font-size: 18px;
}

.search-panel-head p {
  margin: 0;
}

.search-form {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.primary-query {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 8px;
}

.primary-query :deep(.el-input__wrapper) {
  min-height: 44px;
}

.primary-query :deep(.el-button) {
  height: 44px;
}

.search-options-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.advanced-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.advanced-toggle span {
  color: var(--color-text-tertiary);
  font-weight: 400;
}

.elapsed {
  color: var(--color-text-tertiary);
  font-family: var(--font-mono);
  font-size: 11px;
}

.advanced-filters {
  padding: 14px 14px 0;
  margin-top: 2px;
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-md);
}

.advanced-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.advanced-grid :deep(.el-form-item) {
  margin-bottom: 14px;
}

.inline-error {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  margin-top: 12px;
  color: var(--color-danger);
  background: var(--color-danger-soft);
  border: 1px solid color-mix(in srgb, var(--color-danger) 24%, transparent);
  border-radius: var(--radius-md);
  font-size: 13px;
}

.inline-error :deep(.el-button) {
  margin-left: auto;
}

.search-row {
  display: grid;
  grid-template-columns: minmax(280px, 2fr) repeat(3, minmax(130px, 1fr));
  gap: 12px;
}

.query-field {
  min-width: 240px;
}

.external-list,
.tracking-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.external-card,
.tracking-card,
.recommendation-card {
  position: relative;
  padding: 18px;
  padding-right: 48px;
  background: var(--color-surface);
  border: 1px solid var(--line);
  border-radius: 14px;
  box-shadow: var(--shadow-xs);
}

.external-card:has(.result-index) {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  gap: 12px;
  padding-left: 14px;
}

.result-index {
  display: grid;
  width: 26px;
  height: 26px;
  place-items: center;
  color: var(--color-primary);
  font-family: var(--font-mono);
  font-size: 11px;
  font-weight: 700;
  background: var(--color-primary-soft);
  border-radius: var(--radius-sm);
}

.card-main {
  min-width: 0;
}

.result-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
}

.external-card h3,
.tracking-card h3 {
  margin: 10px 0 0;
}

.external-card h3 {
  color: var(--color-text-primary);
  font-size: 17px;
  line-height: 1.45;
  letter-spacing: -0.01em;
}

.date {
  white-space: nowrap;
  font-size: 12px;
}

.authors {
  color: var(--color-text-secondary);
  font-size: 13px;
}

.summary {
  color: var(--color-text-secondary);
  line-height: 1.7;
}

.external-card .summary {
  display: -webkit-box;
  overflow: hidden;
  margin: 10px 0 4px;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

.external-card .summary.expanded {
  display: block;
  overflow: visible;
}

.summary-toggle {
  padding: 0;
  color: var(--color-primary);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.external-card .card-actions {
  margin-top: 14px;
}

.result-skeleton-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.result-skeleton {
  padding: 22px;
  background: var(--color-surface);
  border: 1px solid var(--color-border-subtle);
  border-radius: var(--radius-lg);
}

.result-status {
  display: flex;
  align-items: center;
  gap: 10px;
}

.error-text {
  color: var(--danger);
}

.job-progress {
  max-width: 420px;
  margin: 10px 0;
}

.job-progress span {
  display: block;
  margin-bottom: 6px;
  color: var(--muted);
  font-size: 12px;
}

.tracking-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 20px;
  padding: 10px 0;
  color: var(--muted);
  font-size: 13px;
}

.tracking-results {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin: 10px 0;
}

.mini-result {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: var(--color-bg-subtle);
  border-radius: 9px;
}

.mini-result a,
.recommendation-card a {
  color: var(--accent);
  font-size: 12px;
}

.history-table {
  margin-top: 10px;
}

.section-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 8px;
}

.favorite-check {
  position: absolute;
  top: 18px;
  right: 16px;
}

.batch-progress {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 260px;
  align-items: center;
  gap: 16px;
  padding: 14px;
  background: var(--color-primary-soft);
  border: 1px solid var(--color-primary-border);
  border-radius: var(--radius-md);
}

.batch-progress > div {
  display: flex;
  flex-direction: column;
}

.batch-progress span {
  color: var(--color-text-secondary);
  font-size: 12px;
}

.batch-failures {
  padding: 12px 14px;
  color: var(--color-danger);
  background: var(--color-danger-soft);
  border: 1px solid color-mix(in srgb, var(--color-danger) 24%, transparent);
  border-radius: var(--radius-md);
  font-size: 12px;
}

.batch-failures p {
  margin: 6px 0 0;
  overflow-wrap: anywhere;
}

.seed-box,
.disclaimer {
  padding: 12px 14px;
  border-radius: 10px;
  background: var(--color-warning-soft);
  color: var(--color-warning);
}

.seed-box {
  justify-content: flex-start;
}

.recommendation-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.recommendation-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  border-top: 3px solid var(--accent-2);
}

.recommendation-card.local {
  border-top-color: var(--ok);
}

.recommendation-card h4 {
  margin: 0;
}

.recommendation-card p {
  color: var(--muted);
  line-height: 1.65;
}

@media (max-width: 1050px) {
  .daily-grid {
    grid-template-columns: 1fr;
  }

  .interest-panel {
    grid-template-columns: 1fr auto;
  }

  .interest-copy {
    grid-column: 1 / -1;
  }

  .search-row,
  .recommendation-grid,
  .tracking-results {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 720px) {
  .discovery-hero,
  .search-panel {
    padding: 16px;
  }

  .daily-title,
  .daily-loading {
    align-items: flex-start;
    grid-template-columns: 1fr;
    flex-direction: column;
  }

  .daily-actions,
  .daily-actions :deep(.el-button) {
    width: 100%;
  }

  .interest-panel {
    grid-template-columns: 1fr;
  }

  .interest-copy {
    grid-column: auto;
  }

  .daily-card-top {
    flex-direction: column;
  }

  .daily-summary > :deep(.el-tag) {
    margin-left: 0;
  }

  .daily-detail dl {
    grid-template-columns: 1fr;
  }

  .discovery-hero,
  .search-panel-head,
  .section-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .primary-query,
  .advanced-grid,
  .batch-progress {
    grid-template-columns: 1fr;
  }

  .primary-query :deep(.el-button) {
    width: 100%;
  }

  .search-options-line {
    align-items: flex-start;
    flex-direction: column;
  }

  .section-actions {
    width: 100%;
    justify-content: flex-start;
  }

  .external-card:has(.result-index) {
    grid-template-columns: 1fr;
  }

  .result-index {
    display: none;
  }
}
</style>
