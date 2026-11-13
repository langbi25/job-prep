<template>
  <div class="page">
    <div class="page-header">
      <div class="head-text">
        <h2>Harness 管理</h2>
        <p class="sub">
          工具与技能的唯一管理面。工具是代码（schema + 实现 + 权限），技能是文件（skills/*.md）。
          这里能同时看到「模型能用什么」和「它按什么步骤用」；
          面板里的工具清单就是模型实际拿到的那一份，不另存副本。
        </p>
      </div>
      <div class="head-actions">
        <el-tag :type="healthType" effect="dark" round>健康 {{ ov.health?.score ?? '–' }}</el-tag>
        <el-button :icon="Refresh" circle :loading="loading" @click="loadAll" />
      </div>
    </div>

    <el-row :gutter="12" style="margin-bottom: 12px">
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">{{ ov.tools?.enabled ?? '–' }}<span
            v-if="ov.tools?.disabled" class="stat-of">/{{ ov.tools?.total ?? 0 }}</span></div>
          <div class="stat-label">可调用工具</div>
          <div class="stat-sub">只读 {{ ov.tools?.read_only ?? 0 }} · 写 {{ ov.tools?.write ?? 0 }}
            · <span :class="{ 'num-err': ov.tools?.disabled }">已停用 {{ ov.tools?.disabled ?? 0 }}</span></div>
        </div></el-card>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">{{ ov.skills?.enabled ?? '–' }}<span class="stat-of">/{{ ov.skills?.total ?? 0 }}</span></div>
          <div class="stat-label">启用技能</div>
          <div class="stat-sub">组合 {{ ov.skills?.composite ?? 0 }} · 从未运行 {{ ov.skills?.never_run ?? 0 }}</div>
        </div></el-card>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">{{ ov.window?.calls ?? 0 }}</div>
          <div class="stat-label">近 {{ ov.window?.days ?? 1 }} 天工具调用</div>
          <div class="stat-sub">失败率 {{ pct(ov.window?.error_rate) }} · 均值 {{ ov.window?.avg_ms ?? 0 }}ms</div>
        </div></el-card>
      </el-col>
      <el-col :xs="12" :sm="12" :md="6">
        <el-card shadow="never"><div class="stat">
          <div class="stat-num">
            <span :class="{ 'num-err': ov.health?.counts?.error }">{{ ov.health?.counts?.error ?? 0 }}</span>
            <span class="stat-of">错</span>
            <span class="num-warn">{{ ov.health?.counts?.warn ?? 0 }}</span>
            <span class="stat-of">警</span>
          </div>
          <div class="stat-label">体检问题</div>
          <div class="stat-sub">提示 {{ ov.health?.counts?.info ?? 0 }} 条（不扣分）</div>
        </div></el-card>
      </el-col>
    </el-row>

    <el-tabs v-model="tab">
      <!-- ════════ 工具 ════════ -->
      <el-tab-pane label="工具目录" name="tools">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom: 10px">
          <template #title>
            工具只能经代码进来（schema + 实现 + 读写属性），这个页面不能新建工具——
            能在这里加一个工具，就等于运行时能凭空长出一个能改库的入口。
            写工具在「只查不改」模式下会被工具层直接拒绝，不靠 prompt 约束。
          </template>
        </el-alert>

        <div class="filter-row">
          <el-radio-group v-model="toolFilter" size="small">
            <el-radio-button value="all">全部 {{ toolList.length }}</el-radio-button>
            <el-radio-button value="read">只读 {{ ov.tools?.read_only ?? 0 }}</el-radio-button>
            <el-radio-button value="write">写 {{ ov.tools?.write ?? 0 }}</el-radio-button>
            <el-radio-button value="orphan">无引用 {{ ov.tools?.orphan ?? 0 }}</el-radio-button>
          </el-radio-group>
          <el-input v-model="toolQ" size="small" placeholder="搜名称或说明" clearable style="width: 240px" />
        </div>

        <el-table :data="filteredTools" size="small" row-key="name" border>
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="tool-detail">
                <div class="detail-block">
                  <div class="detail-title">参数</div>
                  <el-table :data="row.params" size="small" border style="max-width: 720px">
                    <el-table-column prop="name" label="字段" width="130">
                      <template #default="{ row: p }">
                        <code>{{ p.name }}</code>
                        <el-tag v-if="p.required" size="small" type="danger" effect="plain" class="mtag">必填</el-tag>
                      </template>
                    </el-table-column>
                    <el-table-column prop="type" label="类型" width="90" />
                    <el-table-column label="说明">
                      <template #default="{ row: p }">
                        <span class="hint">{{ p.description || '—' }}</span>
                        <span v-if="p.enum" class="hint"> ｜ 可选值：{{ p.enum.join(' / ') }}</span>
                      </template>
                    </el-table-column>
                  </el-table>
                  <div v-if="!row.params.length" class="hint">无参数</div>
                </div>
                <div class="detail-block">
                  <div class="detail-title">被这些技能引用</div>
                  <div v-if="row.used_by.length" class="tag-wrap">
                    <el-tag v-for="u in row.used_by" :key="u.skill + u.via" size="small" effect="plain"
                            :type="u.via === 'require' ? 'warning' : 'info'">
                      {{ u.skill }}<span class="via">{{ u.via === 'require' ? '·验收' : u.via === 'step' ? '·段' : '' }}</span>
                    </el-tag>
                  </div>
                  <div v-else class="hint">
                    {{ row.global_exempt ? '全局豁免工具：任何技能都可用（记忆/翻页）' : '没有技能引用它，只在通用 agent 模式下出现' }}
                  </div>
                </div>
                <div class="detail-block" v-if="row.stats?.errors?.length">
                  <div class="detail-title">近 14 天错误分布</div>
                  <el-tag v-for="e in row.stats.errors" :key="e.kind" size="small" type="danger" effect="plain" class="mtag">
                    {{ errZh[e.kind] || e.kind }} ×{{ e.n }}
                  </el-tag>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="工具" width="190">
            <template #default="{ row }">
              <div class="tool-name" :class="{ 'tool-off': !row.enabled }">
                <code>{{ row.name }}</code>
                <el-tag size="small" :type="row.read_only ? 'success' : 'warning'" effect="plain" class="mtag">
                  {{ row.read_only ? '只读' : '写' }}
                </el-tag>
                <el-tag v-if="!row.impl" size="small" type="danger" class="mtag">无实现</el-tag>
                <el-tag v-if="!row.enabled" size="small" type="info" class="mtag"
                        :title="row.disabled_reason">已停用</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="300">
            <template #default="{ row }">
              <div class="tool-desc">{{ row.description }}</div>
            </template>
          </el-table-column>
          <el-table-column label="引用" width="180">
            <template #default="{ row }">
              <div v-if="row.used_by.length" class="tag-wrap">
                <el-tag v-for="u in row.used_by.slice(0, 2)" :key="u.skill + u.via" size="small" effect="plain">
                  {{ u.skill }}
                </el-tag>
                <span v-if="row.used_by.length > 2" class="hint">+{{ row.used_by.length - 2 }}</span>
              </div>
              <span v-else-if="row.global_exempt" class="hint">全局豁免</span>
              <span v-else class="hint">—</span>
            </template>
          </el-table-column>
          <el-table-column label="近 14 天" width="150">
            <template #default="{ row }">
              <span v-if="row.stats?.calls">
                {{ row.stats.calls }} 次 ·
                <span :class="rateClass(row.stats.ok_rate)">{{ pct(row.stats.ok_rate) }}</span>
                <div class="hint">{{ row.stats.avg_ms }}ms 均值</div>
              </span>
              <span v-else class="hint">未调用</span>
            </template>
          </el-table-column>
          <el-table-column label="启用" width="72" align="center">
            <template #default="{ row }">
              <el-switch :model-value="row.enabled" size="small"
                         :loading="toolBusy === row.name"
                         @change="(v) => toggleTool(row, v)" />
            </template>
          </el-table-column>
        </el-table>

        <div class="calls-head">
          <span class="section-title">最近调用</span>
          <div class="spark" v-if="daily.length">
            <div v-for="d in daily" :key="d.day" class="bar"
                 :class="{ 'bar-err': d.errors }"
                 :style="{ height: barH(d.calls) + 'px' }"
                 :title="`${d.day} · ${d.calls} 次，失败 ${d.errors || 0}`" />
          </div>
          <span class="hint">每根柱 = 一天，红色表示当天有失败</span>
        </div>
        <el-table :data="calls" size="small" border>
          <el-table-column prop="ts" label="时间" width="160">
            <template #default="{ row }">{{ (row.ts || '').replace('T', ' ') }}</template>
          </el-table-column>
          <el-table-column prop="tool" label="工具" width="160" />
          <el-table-column prop="source" label="来源" width="130">
            <template #default="{ row }">
              <el-tag size="small" effect="plain" :type="row.source === 'console' ? 'primary' : 'info'">
                {{ row.source }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="skill" label="技能" width="150">
            <template #default="{ row }">
              <span :class="{ hint: row.skill === '-' }">{{ row.skill }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="status" label="结果" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="row.status === 'ok' ? 'success' : 'danger'" effect="plain">
                {{ row.status === 'ok' ? '成功' : (errZh[row.error_kind] || '失败') }}
              </el-tag>
              <el-tag v-if="row.retried" size="small" type="warning" effect="plain" class="mtag">重试</el-tag>
              <el-tag v-if="row.cached" size="small" type="info" effect="plain" class="mtag">缓存</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="latency_ms" label="耗时" width="90">
            <template #default="{ row }">{{ row.latency_ms }}ms</template>
          </el-table-column>
          <el-table-column prop="result_chars" label="结果" width="90">
            <template #default="{ row }">{{ row.result_chars }} 字</template>
          </el-table-column>
          <el-table-column prop="error" label="错误" min-width="220">
            <template #default="{ row }">
              <span v-if="row.error" class="err-text">{{ row.error }}</span>
              <span v-else class="hint">—</span>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ════════ 技能 ════════ -->
      <el-tab-pane label="技能" name="skills">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom: 10px">
          <template #title>
            文件是唯一事实源，这里只是受控编辑器：保存前先解析再体检，有一条 error 就整份拒绝——
            存下一个工具名拼错的版本，下次路由选中它时白名单会被收成空集，而现场只表现为「模型没调工具」。
          </template>
        </el-alert>

        <el-table :data="skillList" size="small" border>
          <el-table-column label="技能" width="260">
            <template #default="{ row }">
              <div class="skill-name">
                <span class="skill-title link" @click="openEditor(row)">{{ row.name }}</span>
                <el-tag v-if="row.steps" size="small" type="primary" effect="plain" class="mtag">组合</el-tag>
                <el-tag v-if="row.disabled" size="small" type="info" class="mtag">已停用</el-tag>
              </div>
              <div class="hint">{{ row.title }}</div>
            </template>
          </el-table-column>
          <el-table-column label="何时用" min-width="240">
            <template #default="{ row }">
              <div class="when-text">{{ row.when || '（未写，自动路由选不中）' }}</div>
            </template>
          </el-table-column>
          <el-table-column label="白名单" width="100">
            <template #default="{ row }">
              <span v-if="row.steps" class="hint">{{ row.segments.length }} 段</span>
              <span v-else>{{ row.tools.length }} 个</span>
              <div class="hint" v-if="row.require_tools?.length">验收 {{ row.require_tools.length }}</div>
            </template>
          </el-table-column>
          <el-table-column label="运行" width="124">
            <template #default="{ row }">
              <span v-if="row.stats.runs">{{ row.stats.runs }} 次</span>
              <span v-else class="hint">从未</span>
              <div class="hint" v-if="row.stats.runs">
                成功 {{ row.stats.ok_runs }} · ¥{{ fmtMoney(row.stats.cost_cny) }}
              </div>
            </template>
          </el-table-column>
          <el-table-column label="体检" width="120">
            <template #default="{ row }">
              <el-tag v-if="countOf(row.issues, 'error')" size="small" type="danger" effect="plain">
                {{ countOf(row.issues, 'error') }} 错
              </el-tag>
              <el-tag v-if="countOf(row.issues, 'warn')" size="small" type="warning" effect="plain" class="mtag">
                {{ countOf(row.issues, 'warn') }} 警
              </el-tag>
              <span v-if="!countOf(row.issues, 'error') && !countOf(row.issues, 'warn')" class="hint">通过</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="170">
            <template #default="{ row }">
              <el-button size="small" type="primary" link @click="openEditor(row)">编辑</el-button>
              <el-button size="small" :type="row.disabled ? 'success' : 'warning'" link @click="toggle(row)">
                {{ row.disabled ? '启用' : '停用' }}
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ════════ 路由试跑 ════════ -->
      <el-tab-pane label="路由试跑" name="route">
        <el-alert type="info" :closable="false" show-icon style="margin-bottom: 10px">
          <template #title>
            技能选错比不选严重得多：选中就会改写这一轮的工具白名单，把该做的事挡在外面。
            所以规则是「覆盖率 &lt; 0.10 不选、前两名分差 &lt; 0.03 不选」。这里能看清每次判定为什么成立。
          </template>
        </el-alert>

        <div class="route-input">
          <el-input v-model="routeQ" placeholder="输入一句用户会说的话，例如：看看我哪里弱，顺手给我出几张卡"
                    clearable @keyup.enter="runRoute" />
          <el-button type="primary" :loading="routing" @click="runRoute">试跑</el-button>
        </div>
        <div class="samples">
          <span class="hint">试试：</span>
          <span v-for="s in samples" :key="s" class="sample" @click="useSample(s)">
            <el-tag size="small" effect="plain">{{ s }}</el-tag>
          </span>
        </div>

        <template v-if="routeRes">
          <el-alert :type="routeRes.picked ? 'success' : 'warning'" :closable="false" show-icon
                    style="margin: 12px 0">
            <template #title>
              <b>{{ routeRes.picked ? `选中「${routeRes.picked}」` : '不套技能，走通用 agent' }}</b>
              <div class="route-reason">{{ routeRes.reason }}</div>
            </template>
          </el-alert>

          <div class="hint" style="margin-bottom: 6px">
            有效词元 {{ routeRes.n_tokens }} 个（阈值 {{ routeRes.thresholds.min_tokens }}）：
            <el-tag v-for="t in routeRes.tokens" :key="t" size="small" effect="plain" class="mtag">{{ t }}</el-tag>
          </div>

          <div v-for="b in routeRes.board" :key="b.name" class="board-row" :class="{ picked: b.picked }">
            <div class="board-name">
              <span>{{ b.name }}</span>
              <el-tag v-if="b.picked" size="small" type="success" effect="plain" class="mtag">命中</el-tag>
              <el-tag v-if="b.tie" size="small" type="warning" effect="plain" class="mtag">打平</el-tag>
              <el-tag v-if="b.disabled" size="small" type="info" class="mtag">停用</el-tag>
              <el-tag v-if="b.broken" size="small" type="danger" effect="plain" class="mtag">缺段</el-tag>
            </div>
            <div class="board-bar">
              <div class="board-fill" :style="{ width: Math.min(100, b.score * 100) + '%' }" />
              <span class="board-score">{{ b.score.toFixed(3) }}</span>
            </div>
            <div class="board-hits">
              <span v-if="b.hits.length" class="hint">命中 {{ b.hits.join('、') }}</span>
              <span v-else class="hint">无词面交集</span>
            </div>
          </div>
        </template>
        <el-empty v-else description="输入一句会真实出现的话，看它会被路由到哪" :image-size="80" />
      </el-tab-pane>

      <!-- ════════ 体检 ════════ -->
      <el-tab-pane label="体检" name="lint">
        <div class="lint-head">
          <div class="lint-score" :class="scoreClass">{{ lintData.score }}</div>
          <div>
            <div class="lint-title">配置健康分</div>
            <div class="hint">
              每条 error 扣 12、warn 扣 4；info 只提示不扣分。
              分数低不代表功能坏了，代表「下一次改动更容易出事」。
            </div>
          </div>
          <div class="lint-counts">
            <el-tag type="danger" effect="plain">{{ lintData.counts?.error || 0 }} 错误</el-tag>
            <el-tag type="warning" effect="plain">{{ lintData.counts?.warn || 0 }} 警告</el-tag>
            <el-tag type="info" effect="plain">{{ lintData.counts?.info || 0 }} 提示</el-tag>
          </div>
        </div>

        <el-table :data="sortedIssues" size="small" border>
          <el-table-column label="级别" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="levelType(row.level)" effect="plain">{{ levelZh[row.level] }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="skill" label="对象" width="150">
            <template #default="{ row }">
              <span :class="{ hint: row.skill === '-' }">{{ row.skill }}</span>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="260">
            <template #default="{ row }">
              <code class="code">{{ row.code }}</code>
              <span class="issue-msg">{{ row.msg }}</span>
            </template>
          </el-table-column>
          <el-table-column label="" width="70">
            <template #default="{ row }">
              <el-button v-if="row.skill !== '-'" size="small" type="primary" link
                         @click="editByName(row.skill)">去修</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 技能编辑器 -->
    <el-drawer v-model="drawer" :size="drawerSize" :title="`技能 · ${editing.name}`" destroy-on-close>
      <el-tabs v-model="drawerTab" class="drawer-tabs">
        <el-tab-pane label="编辑" name="edit">
          <div class="editor-head">
            <code class="hint">{{ editing.path }}</code>
            <el-tag v-if="currentVersion" size="small" effect="plain">当前 v{{ currentVersion }}</el-tag>
          </div>

          <el-input v-model="editing.text" type="textarea" :rows="18" spellcheck="false" class="editor" />
          <el-input v-model="editMessage" size="small" class="msg-input" maxlength="200"
                    placeholder="这次改了什么？（写进版本历史；空着也能存，但以后只能靠 diff 猜）" />

          <div v-if="saveIssues.length" class="issues">
            <div class="issues-title">
              校验结果
              <span class="hint">（error 会阻止保存，warn 只提示）</span>
            </div>
            <div v-for="(i, k) in saveIssues" :key="k" class="issue-line">
              <el-tag size="small" :type="levelType(i.level)" effect="plain">{{ levelZh[i.level] }}</el-tag>
              <code class="code">{{ i.code }}</code>
              <span class="issue-msg">{{ i.msg }}</span>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane :label="`历史 ${versions.length}`" name="history">
          <el-alert v-if="dirty" type="warning" :closable="false" show-icon class="dirty-alert">
            <template #title>
              磁盘上的内容不在历史里：它是被平台外改的（手改 md / 切了分支 / 脚本生成）。
              在这里点一次「校验并保存」就会记为一个新版本，之后才能 diff。
            </template>
          </el-alert>

          <el-alert v-if="unversionedRuns" type="info" :closable="false" show-icon class="dirty-alert">
            <template #title>
              另有 {{ unversionedRuns }} 次运行发生在版本管理接入之前（没有版本号），
              不计入下面的分版本统计——所以「总运行次数」会比这几个版本加起来更多。
            </template>
          </el-alert>

          <div class="ver-head">
            <span class="hint">对比</span>
            <el-select v-model="diffA" size="small" style="width: 120px" placeholder="旧">
              <el-option v-for="v in versions" :key="v.version" :value="v.version" :label="`v${v.version}`" />
            </el-select>
            <span class="hint">→</span>
            <el-select v-model="diffB" size="small" style="width: 120px" placeholder="新">
              <el-option v-for="v in versions" :key="v.version" :value="v.version" :label="`v${v.version}`" />
            </el-select>
            <el-button size="small" type="primary" plain :loading="diffLoading" @click="loadDiff">看差异</el-button>
          </div>

          <el-table :data="versions" size="small" border class="ver-table">
            <el-table-column label="版本" width="66">
              <template #default="{ row }">
                <span class="ver-no">v{{ row.version }}</span>
              </template>
            </el-table-column>
            <el-table-column label="时间" width="132">
              <template #default="{ row }"><span class="hint">{{ shortTime(row.created_at) }}</span></template>
            </el-table-column>
            <el-table-column label="来源" width="72">
              <template #default="{ row }">
                <el-tag size="small" effect="plain" :type="srcType(row.source)">{{ srcZh[row.source] || row.source }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="改动说明" min-width="150">
              <template #default="{ row }">
                <span :class="{ hint: !row.message }">{{ row.message || '（未填写）' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="跑过" width="86">
              <template #default="{ row }">
                <template v-if="row.runs">
                  <span>{{ row.runs }} 次</span>
                  <div class="hint">成功 {{ Math.round((row.ok_rate || 0) * 100) }}%</div>
                </template>
                <span v-else class="hint">没跑过</span>
              </template>
            </el-table-column>
            <el-table-column label="表现" width="132">
              <template #default="{ row }">
                <template v-if="row.runs">
                  <span>{{ row.avg_ms || 0 }}ms / 次</span>
                  <div class="hint">合计 ¥{{ fmtMoney(row.cost_cny) }}</div>
                </template>
                <span v-else class="hint">—</span>
              </template>
            </el-table-column>
            <el-table-column label="" width="92">
              <template #default="{ row }">
                <el-button v-if="row.version !== currentVersion" size="small" type="warning" link
                           @click="revertTo(row)">回滚</el-button>
                <span v-else class="hint">当前</span>
              </template>
            </el-table-column>
          </el-table>

          <div v-if="diff" class="diff">
            <div class="diff-head">
              <span class="diff-title">v{{ diff.a.version }} → v{{ diff.b.version }}</span>
              <el-tag size="small" type="success" effect="plain">+{{ diff.stats.added }}</el-tag>
              <el-tag size="small" type="danger" effect="plain">−{{ diff.stats.removed }}</el-tag>
              <span v-if="diff.truncated" class="hint">已截断</span>
            </div>
            <div class="diff-body">
              <div v-for="(r, i) in diff.rows" :key="i" :class="['diff-row', r.op]">
                <template v-if="r.op === 'skip'">
                  <span class="diff-skip">⋯ 未变的 {{ r.n }} 行 ⋯</span>
                </template>
                <template v-else>
                  <span class="ln">{{ r.a ?? '' }}</span>
                  <span class="ln">{{ r.b ?? '' }}</span>
                  <span class="sign">{{ r.op === 'add' ? '+' : (r.op === 'del' ? '−' : ' ') }}</span>
                  <span class="tx">{{ r.text }}</span>
                </template>
              </div>
            </div>
          </div>
          <div v-else class="hint empty-diff">选两个版本看差异。相同段落会折叠成一行，只留改动前后两行上下文。</div>
        </el-tab-pane>
      </el-tabs>

      <template #footer>
        <div class="editor-foot">
          <span class="hint">保存前会先解析再体检，同时写入版本历史</span>
          <div>
            <el-button @click="drawer = false">关闭</el-button>
            <el-button v-if="drawerTab === 'edit'" type="primary" :loading="saving" @click="save">校验并保存</el-button>
          </div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import api from '../api'

const ov = ref({})
const toolList = ref([])
const skillList = ref([])
const lintData = ref({ issues: [], counts: {}, score: 100 })
const calls = ref([])
const daily = ref([])
const tab = ref('tools')
const loading = ref(false)

const toolFilter = ref('all')
const toolQ = ref('')

const drawer = ref(false)
// 固定 780px 时在窄窗口下会溢出到视口左侧（标题被裁一半），按可用宽度收一下
const drawerSize = ref('780px')
const drawerTab = ref('edit')
const editing = reactive({ name: '', text: '', path: '' })
const saving = ref(false)
const saveIssues = ref([])
const editMessage = ref('')

// 版本历史
const versions = ref([])
const unversionedRuns = ref(0)
const dirty = ref(false)          // 磁盘内容不在历史里（平台外改过）
const diffA = ref(null)
const diffB = ref(null)
const diff = ref(null)
const diffLoading = ref(false)
const currentVersion = computed(() => versions.value[0]?.version || null)

const routeQ = ref('')
const routeRes = ref(null)
const routing = ref(false)

const errZh = {
  invalid_args: '参数错', unknown_tool: '未知工具', timeout: '超时',
  bad_json: '参数非 JSON', failed: '执行失败', missing: '缺结果',
}
const levelZh = { error: '错误', warn: '警告', info: '提示' }
const srcZh = { save: '保存', toggle: '启停', revert: '回滚', import: '导入' }
function srcType(s) {
  if (s === 'revert') return 'warning'
  if (s === 'toggle' || s === 'import') return 'info'
  return 'primary'
}
function shortTime(iso) {
  return (iso || '').replace('T', ' ').slice(5, 16)
}
const samples = [
  '看看我哪里弱，顺手给我出几张卡',
  '帮我把这份面经收进去',
  '把这份复盘变成题目和卡片',
  '今天天气怎么样',
]

const healthType = computed(() => {
  const s = ov.value.health?.score ?? 100
  return s >= 90 ? 'success' : s >= 70 ? 'warning' : 'danger'
})
const scoreClass = computed(() => {
  const s = lintData.value.score ?? 100
  return s >= 90 ? 'good' : s >= 70 ? 'mid' : 'bad'
})
const sortedIssues = computed(() => {
  const rank = { error: 0, warn: 1, info: 2 }
  return [...(lintData.value.issues || [])].sort(
    (a, b) => (rank[a.level] ?? 3) - (rank[b.level] ?? 3))
})
const filteredTools = computed(() => toolList.value.filter((t) => {
  if (toolFilter.value === 'read' && !t.read_only) return false
  if (toolFilter.value === 'write' && t.read_only) return false
  if (toolFilter.value === 'orphan' && (t.used_by.length || t.global_exempt)) return false
  const q = toolQ.value.trim().toLowerCase()
  if (q && !t.name.toLowerCase().includes(q) && !(t.description || '').toLowerCase().includes(q)) return false
  return true
}))

function pct(v) {
  if (v === null || v === undefined) return '—'
  return `${(Number(v) * 100).toFixed(v >= 0.995 ? 0 : 1)}%`
}
// 技能一次几分钱，两位小数会全显示成 0.00——小于 0.01 元时保留到厘
function fmtMoney(v) {
  const n = Number(v || 0)
  if (!n) return '0'
  return n < 0.01 ? n.toFixed(4) : n.toFixed(2)
}

const toolBusy = ref('')
async function toggleTool(row, enabled) {
  let reason = ''
  if (!enabled) {
    // 停用要问原因：将来看到"这个工具错了 12 次"时，
    // 有没有一句话解释决定了这条记录是有用的证据还是一堆噪音
    try {
      const r = await ElMessageBox.prompt(
        '停用后模型看不到它、技能白名单会过滤它、执行层也会拒绝并记一条台账。为什么停用？',
        `停用 ${row.name}`,
        { inputPlaceholder: '例如：正在改坏数据 / 上游接口挂了 / 准备下线', confirmButtonText: '停用' },
      )
      reason = r.value || ''
    } catch {
      return            // 取消：开关保持原状
    }
  }
  toolBusy.value = row.name
  try {
    const res = await api.post(`/api/harness/tools/${row.name}/toggle`, { enabled, reason })
    row.enabled = enabled
    row.disabled_reason = res.reason || ''
    const aff = (res.affected_skills || []).filter(Boolean)
    if (!enabled && aff.length) {
      ElMessage.warning(`已停用；${aff.length} 个技能引用了它：${aff.join('、')}——它们的验收会开始失败`)
    } else {
      ElMessage.success(enabled ? '已启用' : '已停用')
    }
    load()          // 概览里的停用计数要跟着变
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '开关没生效')
  } finally {
    toolBusy.value = ''
  }
}
function rateClass(v) {
  if (v === null || v === undefined) return ''
  return v >= 0.95 ? 'ok-rate' : v >= 0.8 ? 'mid-rate' : 'bad-rate'
}
function countOf(issues, level) {
  return (issues || []).filter((i) => i.level === level).length
}
function levelType(l) {
  return l === 'error' ? 'danger' : l === 'warn' ? 'warning' : 'info'
}
function barH(n) {
  const max = Math.max(...daily.value.map((d) => d.calls || 0), 1)
  return Math.max(3, Math.round(((n || 0) / max) * 36))
}

async function loadAll() {
  loading.value = true
  try {
    // 一次拿全：服务端只构建一份快照、只开一个连接。
    // 之前并发 5 个接口，每个都在重算同一份技能/工具目录。
    const d = await api.get('/api/harness/board', { params: { limit: 30 } })
    ov.value = d.overview || {}
    toolList.value = d.tools || []
    skillList.value = d.skills || []
    lintData.value = d.lint || { issues: [], counts: {} }
    calls.value = d.calls || []
    daily.value = d.daily || []
  } catch (e) { /* 拦截器已提示 */ } finally {
    loading.value = false
  }
}

async function loadVersions(name) {
  const v = await api.get(`/api/harness/skills/${name}/versions`)
  versions.value = v.versions || []
  dirty.value = !!v.dirty
  unversionedRuns.value = v.unversioned_runs || 0
  if (versions.value.length >= 2) {
    diffB.value = versions.value[0].version
    diffA.value = versions.value[1].version
  } else if (versions.value.length === 1) {
    diffA.value = diffB.value = versions.value[0].version
  }
}

async function openEditor(row) {
  try {
    const d = await api.get(`/api/harness/skills/${row.name}`)
    Object.assign(editing, { name: d.name, text: d.text, path: d.path })
    saveIssues.value = []
    editMessage.value = ''
    drawerTab.value = 'edit'
    diff.value = null
    await loadVersions(row.name)
    drawer.value = true
  } catch (e) { /* 拦截器已提示 */ }
}

function editByName(name) {
  const row = skillList.value.find((s) => s.name === name)
  if (row) openEditor(row)
}

async function save() {
  saving.value = true
  try {
    const res = await api.put(`/api/harness/skills/${editing.name}`,
      { text: editing.text, message: editMessage.value })
    saveIssues.value = res.issues || []
    if (res.ok) {
      ElMessage.success(
        res.created ? '已新建技能'
          : res.unchanged ? '内容与上一版相同，没有产生新版本'
            : `已保存为 v${res.version}`)
      await loadAll()
      editMessage.value = ''
      await loadVersions(editing.name)
    } else {
      ElMessage.warning(`${res.errors?.length || 0} 处必须修正，未保存`)
    }
  } catch (e) { /* 拦截器已提示 */ } finally {
    saving.value = false
  }
}

async function toggle(row) {
  try {
    await api.post(`/api/harness/skills/${row.name}/toggle`, { enabled: row.disabled })
    ElMessage.success(row.disabled ? `已启用 ${row.name}` : `已停用 ${row.name}`)
    await loadAll()
  } catch (e) { /* 拦截器已提示 */ }
}

async function loadDiff() {
  if (diffA.value == null || diffB.value == null) return
  if (diffA.value === diffB.value) {
    diff.value = null
    ElMessage.info('选了两个相同的版本')
    return
  }
  diffLoading.value = true
  try {
    diff.value = await api.get(`/api/harness/skills/${editing.name}/diff`,
      { params: { a: diffA.value, b: diffB.value } })
  } catch (e) { /* 拦截器已提示 */ } finally {
    diffLoading.value = false
  }
}

async function revertTo(row) {
  try {
    await ElMessageBox.confirm(
      `把正文恢复为 v${row.version}？回滚会记成一个新版本，历史不会被改写。`,
      `回滚 ${editing.name}`,
      { type: 'warning', confirmButtonText: '回滚', cancelButtonText: '取消' })
  } catch (e) { return }
  try {
    const r = await api.post(`/api/harness/skills/${editing.name}/revert`, { version: row.version })
    ElMessage.success(`已恢复 v${row.version} 的内容，记为 v${r.version}`)
    editing.text = r.text
    saveIssues.value = []
    await loadAll()
    await loadVersions(editing.name)
  } catch (e) { /* 拦截器已提示 */ }
}

async function runRoute() {
  if (!routeQ.value.trim()) { ElMessage.warning('先输入一句话'); return }
  routing.value = true
  try {
    routeRes.value = await api.post('/api/harness/route', { query: routeQ.value })
  } catch (e) { /* 拦截器已提示 */ } finally {
    routing.value = false
  }
}

function useSample(s) {
  routeQ.value = s
  runRoute()
}

onMounted(() => {
  drawerSize.value = `${Math.min(780, Math.max(360, window.innerWidth - 60))}px`
  loadAll()
})
</script>

<style scoped>
.page { padding: 24px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; gap: 12px; }
.head-text { flex: 1; min-width: 0; }
.page-header h2 { margin: 0; font-size: 22px; }
.sub { color: #909399; font-size: 12px; margin: 4px 0 0; max-width: 900px; line-height: 1.6; }
.head-actions { display: flex; align-items: center; gap: 8px; flex: none; }
.hint { color: #909399; font-size: 12px; }
.stat { text-align: center; padding: 4px 0; }
.stat-num { font-size: 24px; font-weight: 700; color: #303133; }
.stat-of { font-size: 13px; font-weight: 400; color: #c0c4cc; margin: 0 4px 0 2px; }
.stat-label { color: #606266; font-size: 12px; margin-top: 2px; }
.stat-sub { color: #c0c4cc; font-size: 11px; margin-top: 2px; }
.num-err { color: #f56c6c; }
.num-warn { color: #e6a23c; margin-left: 6px; }
.filter-row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 12px; }
.mtag { font-size: 10px; margin-left: 4px; }
.tag-wrap { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.via { opacity: 0.6; font-size: 10px; }
.tool-name { display: flex; align-items: center; flex-wrap: wrap; gap: 2px; }
/* 停用的工具整行压暗：一眼扫过去就知道它不在可用集合里，而不是读标签才知道 */
.tool-off { opacity: 0.55; }
.tool-off code { text-decoration: line-through; }
.tool-desc { font-size: 12px; color: #606266; line-height: 1.6; }
.tool-detail { padding: 4px 8px 10px 40px; display: flex; flex-direction: column; gap: 12px; }
.detail-title { font-size: 12px; color: #303133; font-weight: 600; margin-bottom: 4px; }
.ok-rate { color: #67c23a; }
.mid-rate { color: #e6a23c; }
.bad-rate { color: #f56c6c; }
.err-text { color: #f56c6c; font-size: 12px; }
.calls-head { display: flex; align-items: center; gap: 12px; margin: 16px 0 8px; flex-wrap: wrap; }
.section-title { font-size: 14px; font-weight: 600; color: #303133; }
.spark { display: flex; align-items: flex-end; gap: 2px; height: 38px; }
.bar { width: 8px; background: #c6e2ff; border-radius: 2px 2px 0 0; }
.bar-err { background: #fbc4c4; }

.skill-name { display: flex; align-items: center; gap: 4px; }
.skill-title { font-weight: 600; font-size: 13px; }
.link { color: #409eff; cursor: pointer; }
.link:hover { text-decoration: underline; }
.when-text { font-size: 12px; color: #606266; line-height: 1.6; }

.route-input { display: flex; gap: 8px; }
.samples { display: flex; align-items: center; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
.sample { cursor: pointer; display: inline-block; }
.route-reason { font-size: 12px; margin-top: 4px; line-height: 1.6; }
.board-row { display: grid; grid-template-columns: 180px minmax(120px, 1fr); gap: 4px 10px;
  align-items: center; padding: 7px 8px; border-radius: 4px; }
.board-row + .board-row { border-top: 1px dashed #ebeef5; }
.board-row.picked { background: #f0f9eb; }
.board-name { font-size: 13px; display: flex; align-items: center; gap: 2px; flex-wrap: wrap; }
.board-bar { position: relative; height: 14px; background: #f5f7fa; border-radius: 7px; overflow: hidden; }
.board-fill { height: 100%; background: #a0cfff; border-radius: 7px; }
.picked .board-fill { background: #95d475; }
.board-score { position: absolute; right: 6px; top: -1px; font-size: 11px; color: #606266; }
.board-hits { font-size: 11px; grid-column: 1 / -1; }

.lint-head { display: flex; align-items: center; gap: 16px; margin-bottom: 14px; }
.lint-score { font-size: 34px; font-weight: 700; line-height: 1; }
.lint-score.good { color: #67c23a; }
.lint-score.mid { color: #e6a23c; }
.lint-score.bad { color: #f56c6c; }
.lint-title { font-size: 13px; font-weight: 600; color: #303133; }
.lint-counts { margin-left: auto; display: flex; gap: 6px; }
.code { font-size: 11px; color: #909399; }

.editor-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; gap: 8px; }
.editor :deep(textarea) { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.7; }
.issues { margin-top: 12px; border-top: 1px solid #ebeef5; padding-top: 10px; }
.issues-title { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.issue-line { display: flex; align-items: baseline; gap: 8px; padding: 3px 0; }
.issue-msg { font-size: 12px; color: #606266; line-height: 1.5; margin-left: 6px; }
.editor-foot { display: flex; justify-content: space-between; align-items: center; }

/* 抽屉内的版本历史 */
.drawer-tabs :deep(.el-tabs__header) { margin-bottom: 12px; }
.msg-input { margin-top: 8px; }
.dirty-alert { margin-bottom: 10px; }
.ver-head { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.ver-table { margin-bottom: 12px; }
.ver-no { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; color: #303133; }

.diff { border: 1px solid #ebeef5; border-radius: 4px; overflow: hidden; }
.diff-head { display: flex; align-items: center; gap: 8px; padding: 8px 10px; background: #fafafa;
  border-bottom: 1px solid #ebeef5; }
.diff-title { font-size: 13px; font-weight: 600; }
.diff-body { max-height: 42vh; overflow: auto; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.65; }
.diff-row { display: grid; grid-template-columns: 34px 34px 14px 1fr; gap: 0; }
.diff-row.add { background: #f0f9eb; }
.diff-row.del { background: #fef0f0; }
.diff-row .ln { color: #c0c4cc; text-align: right; padding: 0 4px; user-select: none;
  border-right: 1px solid #f2f6fc; }
.diff-row .sign { text-align: center; color: #909399; user-select: none; }
.diff-row.add .sign { color: #67c23a; }
.diff-row.del .sign { color: #f56c6c; }
.diff-row .tx { padding: 0 6px; white-space: pre-wrap; word-break: break-word; color: #303133; }
/* 折叠行没有行号，必须跨满整行——只写 block 会被塞进第一个 34px 的行号列，变成竖排单字 */
.diff-skip { grid-column: 1 / -1; display: block; padding: 2px 10px; color: #b1b3b8;
  background: #fafafa; font-size: 11px; }
.empty-diff { padding: 14px 2px; line-height: 1.7; }
</style>
