import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Home() {
  const [o, setO] = useState(null)
  const [w, setW] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    Promise.all([api.overview(), api.wall()])
      .then(([ov, wall]) => { setO(ov); setW(wall) })
      .catch(e => setErr(e.message))
  }, [])
  if (err) return <p className="err">{err}</p>
  if (!o) return <p className="muted">加载总览…</p>
  const cards = [
    ['等候', o.等候], ['办理中', o.办理中], ['过号', o.过号],
    ['已办结', o.已办结], ['已作废', o.已作废], ['开放窗口', o.开放窗口],
  ]
  const probe = o.墙面核对
  const sameBucket =
    w && o.等候 === w.等候总数 && o.过号 === w.过号总数
  const sameFlush = probe && w
    && probe.重放批次 === w.重放批次
  const latest = probe?.审计最新 || w?.审计最新
  return (
    <div className={surface_panel ? "page" : "page"}>
      <header className="hero">
        <p className="eyebrow">网点队列物化</p>
        <h1>叫号条</h1>
        <p className="lede">
          营业日 {o.营业日} · {o.已封口 ? '已封口' : '营业中'}。取号入队，窗口叫号推进，过号可回收重叫，当日封口后禁写。
        </p>
      </header>
      <div className="stat-grid">
        {cards.map(([k, v]) => (
          <div className="stat" key={k}><span>{k}</span><strong>{v}</strong></div>
        ))}
      </div>
      <section className="panel">
        <h2>等候桶核对</h2>
        <p className={sameBucket && sameFlush ? 'muted' : 'err'}>
          总览等候 {o.等候} / 过号 {o.过号} · 墙面等候 {w?.等候总数 ?? '—'} / 过号 {w?.过号总数 ?? '—'}
          {sameBucket ? ' · 与墙面同源一致' : ' · 与墙面不一致'}
        </p>
        <p className={sameFlush ? 'muted' : 'err'}>
          重放批次：总览 {probe?.重放批次?.slice(0, 12) ?? '—'}… · 墙面 {w?.重放批次?.slice(0, 12) ?? '—'}…
          {sameFlush ? ' · 同一批次' : ' · 不同批次（可能半刷）'}
        </p>
        <p className="muted">
          墙面真源：{w?.真源 || '—'} · 对齐灯 {w?.对齐?.灯 ? '亮' : '灭'} ·
          可叫 {w?.可叫 ? `是（${w.可叫分栏.length} 分栏）` : '否'}
        </p>
        <p className="muted">
          高亮核对：{w?.高亮核对?.一致 ? '一致' : `不一致${w?.高亮核对?.问题?.length ? `（${w.高亮核对.问题.length} 项）` : ''}`}
          {latest ? ` · 最近墙刷新 ${latest.时刻}（${latest.触发}）` : ''}
        </p>
        {!w?.对齐?.灯 && w?.对齐?.说明 && <p className="err">{w.对齐.说明}</p>}
      </section>
      <section className="panel">
        <h2>窗口现况</h2>
        <ul className="plain">
          {o.窗口.map(win => (
            <li key={win.id}>
              {win.名称} · {win.开放 ? '开放' : '关闭'} · {win.有在办 ? '有在办号' : '空闲'}
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
