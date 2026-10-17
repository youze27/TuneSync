<template>
  <el-form label-width="108px" label-position="left">
    <el-divider content-position="left">
      搜索接口
      <span class="muted" style="font-weight: 400">模板变量：<code>{kw}</code> 搜索词</span>
    </el-divider>

    <el-form-item label="搜索 URL" required>
      <el-input v-model="cfg.search" placeholder="http://127.0.0.1:3001/search?keywords={kw}&limit=20" clearable />
    </el-form-item>

    <el-form-item label="列表路径">
      <el-input v-model="cfg.list_path" placeholder="data.songs" />
      <div class="muted" style="font-size: 12px">点号路径，定位到歌曲数组；<code>$</code> 表示响应本身就是数组</div>
    </el-form-item>

    <el-divider content-position="left">字段映射</el-divider>

    <el-row :gutter="12">
      <el-col :span="12">
        <el-form-item label="ID 字段">
          <el-input v-model="cfg.id_field" placeholder="id" />
        </el-form-item>
      </el-col>
      <el-col :span="12">
        <el-form-item label="标题字段">
          <el-input v-model="cfg.title_field" placeholder="name" />
        </el-form-item>
      </el-col>
      <el-col :span="12">
        <el-form-item label="艺术家字段">
          <el-input v-model="cfg.artist_field" placeholder="artist" />
        </el-form-item>
      </el-col>
      <el-col :span="12">
        <el-form-item label="专辑字段">
          <el-input v-model="cfg.album_field" placeholder="album" />
        </el-form-item>
      </el-col>
    </el-row>

    <template v-if="kind !== 'lx'">
      <el-divider content-position="left">
        下载链接接口
        <span class="muted" style="font-weight: 400">
          变量：<code>{id}</code> <code>{quality}</code>
        </span>
      </el-divider>

      <el-form-item label="取链 URL" required>
        <el-input v-model="cfg.url" placeholder="http://127.0.0.1:3001/song/url?id={id}&quality={quality}" clearable />
      </el-form-item>

      <el-form-item label="链接取值路径">
        <div class="url-paths">
          <div v-for="(p, i) in cfg.url_paths" :key="i" class="url-path-row">
            <el-input v-model="cfg.url_paths[i]" placeholder="data.url" />
            <el-button :icon="Delete" text type="danger" @click="removePath(i)" />
          </div>
          <el-button :icon="Plus" size="small" @click="addPath">添加取值路径</el-button>
          <div class="muted" style="font-size: 12px; width: 100%; margin-top: 2px">
            按顺序尝试，命中第一个非空值；至少保留一条。
          </div>
        </div>
      </el-form-item>

      <el-form-item label="音质">
        <el-select v-model="cfg.quality" style="width: 200px">
          <el-option v-for="q in QUALITIES" :key="q" :label="q" :value="q" />
        </el-select>
        <span class="muted" style="margin-left: 10px; font-size: 12px">请求链接时填入 {quality}</span>
      </el-form-item>
    </template>

    <template v-else>
      <el-divider content-position="left">
        LX 音源脚本
        <span class="muted" style="font-weight: 400">取链接由脚本在 Node 沙箱里完成</span>
      </el-divider>

      <el-form-item label="脚本" required>
        <el-input
          v-model="cfg.script"
          type="textarea"
          :rows="6"
          placeholder="粘贴 lx-music 音源脚本文本，或填脚本 URL（http(s):// 开头，自动拉取并缓存 10 分钟）"
          class="mono"
        />
        <div class="muted" style="font-size: 12px; margin-top: 4px">
          兼容 lx-music 社区音源脚本；脚本在沙箱中运行，只能使用 lx.* 接口
        </div>
      </el-form-item>

      <el-form-item label="脚本平台">
        <el-select v-model="cfg.platform" style="width: 220px">
          <el-option v-for="p in LX_PLATFORMS" :key="p.value" :label="p.label" :value="p.value" />
        </el-select>
        <span class="muted" style="margin-left: 10px; font-size: 12px">调用脚本时传给 info.source</span>
      </el-form-item>

      <el-form-item label="音质">
        <div class="muted" style="font-size: 12px">
          由脚本声明的 qualitys 决定：flac → flac24bit/flac/320k 逐级降级，320 → 320k/128k
        </div>
      </el-form-item>
    </template>
  </el-form>
</template>

<script setup>
import { Delete, Plus } from '@element-plus/icons-vue'

const QUALITIES = ['flac', '320', '128', '96']
const MIN_URL_PATHS = 1
const LX_PLATFORMS = [
  { value: 'wy', label: 'wy · 网易云' },
  { value: 'kg', label: 'kg · 酷狗' },
  { value: 'kw', label: 'kw · 酷我' },
  { value: 'tx', label: 'tx · QQ 音乐' },
  { value: 'mg', label: 'mg · 咪咕' },
]

/** 直接编辑调用方传入的 cfg 草稿对象（弹窗内的 reactive 副本）。 */
const props = defineProps({
  cfg: { type: Object, required: true },
  kind: { type: String, default: 'http' },
})

function addPath() {
  props.cfg.url_paths = [...(props.cfg.url_paths || []), '']
}

function removePath(i) {
  const next = (props.cfg.url_paths || []).filter((_, idx) => idx !== i)
  props.cfg.url_paths = next.length >= MIN_URL_PATHS ? next : ['']
}
</script>

<style scoped>
.url-paths {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: flex-start;
}
.url-path-row {
  display: flex;
  gap: 6px;
  width: 100%;
  max-width: 520px;
}
code {
  background: #f2f4f7;
  padding: 0 4px;
  border-radius: 3px;
  font-size: 12px;
}
</style>
