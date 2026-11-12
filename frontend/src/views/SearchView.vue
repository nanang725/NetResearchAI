<template>
  <div class="search-container">
    <h2>智能文献检索</h2>
    <p class="subtitle">输入关键词，系统将基于文本切块（Chunking）返回真实原文与页码。</p>
    
    <div class="search-bar">
      <el-input 
        v-model="query" 
        placeholder="请输入搜索词，例如：Science、Network、TCP..." 
        @keyup.enter="handleSearch"
        clearable
      />
      <el-button type="primary" @click="handleSearch" :loading="loading">
        搜索证据
      </el-button>
    </div>

    <!-- 结果列表 -->
    <div class="results-list" v-if="results.length > 0">
      <el-card v-for="(item, index) in results" :key="item.chunk_id" class="result-card" shadow="hover">
        <template #header>
          <div class="card-header">
            <span class="paper-title">{{ item.paper_title }}</span>
            <el-tag type="info">第 {{ item.page_number }} 页</el-tag>
          </div>
        </template>
        <div class="evidence-text">{{ item.text }}</div>
      </el-card>
    </div>
    
    <!-- 空状态 -->
    <el-empty v-else-if="hasSearched && !loading" description="未找到包含该关键词的文献证据" />
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { searchPapers } from '../api/papers'
import { ElMessage } from 'element-plus'

const query = ref('')
const loading = ref(false)
const hasSearched = ref(false)
const results = ref<any[]>([])

const handleSearch = async () => {
  if (!query.value.trim()) {
    ElMessage.warning('请输入搜索词')
    return
  }
  
  loading.value = true
  hasSearched.value = true
  try {
    const res: any = await searchPapers(query.value)
    // 根据后端的成功返回结构，通常数据在 res.data.data 里
    results.value = res.data?.data || res.data || []
  } catch (error) {
    ElMessage.error('检索失败，请检查后端服务')
    console.error(error)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.search-container {
  padding: 24px;
  max-width: 1000px;
  margin: 0 auto;
}
.subtitle {
  color: #666;
  margin-bottom: 24px;
}
.search-bar {
  display: flex;
  gap: 12px;
  margin-bottom: 32px;
}
.results-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.paper-title {
  font-weight: bold;
  color: #2c3e50;
}
.evidence-text {
  line-height: 1.6;
  color: #333;
  white-space: pre-wrap;
  font-size: 14px;
}
</style>