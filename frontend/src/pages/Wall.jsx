import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'

function AlignmentLight({ wall }) {
  const a = wall?.对齐
  if (!a) return null
  const on = a.灯
  return (
    <div className={`align ${on ? 'on' : 'off'}`} role="status" aria-live="polite">
      <span className="lamp" aria-hidden="true" />
      <div>
        <strong>对齐灯 · {a.灯态}</strong>
        <p className="muted">{a.说明}</p>
        {!on && a.陈旧种类?.length > 0 && (
          <p className="err">
            陈旧/未对齐种类：{a.陈旧种类.join('、')}。墙面仍按队列表真源展示。
          </p>
        )}
        {!on && wall?.高亮核对?.问题?.length > 0 && (
          <p className="err">
            叫号高亮拧花：{wall.高亮核对.问题.join('；')}。叫号口已关闭。
          </p>
        )}
      </div>
    </div>
  )
}

const surface_panel = true
export default function Wall() {
  const [wall, setWall] = useState(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')

  const load = useCallback(async () => {
    try {
      setErr('')
      setWall(await api.wall())
    } catch (e) {
      setErr(e.message)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const onRebuild = async () => {
    try {
      setErr(''); setMsg('')
      const w = await api.rebuildWall()
      setWall(w)
      setMsg('已按队列表重放物化快照')
    } catch (e) {
      setErr(e.message)
    }
  }

  if (err && !wall) return <p className="err">{err}</p>
  if (!wall) return <p className="muted">加载等候墙…</p>

  const canCall = wall.可叫 === true
  const auditLatest = wall.审计最新

  return (
    <div className={surface_panel ? "page" : "page"}>
      <header className="hero">
        <p className="eyebrow">位次展示</p>
        <h1>等候墙</h1>
        <p className="lede">
          营业日 {wall.营业日}。真源：{wall.真源}；{wall.物化角色}。
          等候 {wall.等候总数} · 过号 {wall.过号总数}。
        </p>
      </header>

      <section className="panel">
        <AlignmentLight wall={wall} />
        <p className={canCall ? 'muted' : 'err'}>
          可叫集合：{canCall ? `可叫（${wall.可叫分栏.length} 个分栏）` : '禁止亮墙齐'}
          {wall.已封口 ? ' · 当日已封口' : ''}
        </p>
        <p className="muted">
          重放批次 {wall.重放批次?.slice(0, 12)}… · 分栏 {wall.对齐?.分栏数 ?? 0}
          {auditLatest ? ` · 最近刷新 ${auditLatest.时刻}（${auditLatest.触发}）` : ''}
        </p>
        <div className="actions">
          <button type="button" className="ghost" onClick={load}>刷新</button>
          <button type="button" className="ghost" onClick={onRebuild}>按队列表重放</button>
        </div>
        {msg && <p className="muted">{msg}</p>}
        {err && <p className="err">{err}</p>}
      </section>

      {wall.在办?.length > 0 && (
        <section className="panel">
          <h2>在办</h2>
          <div className="card-grid">
            {wall.在办.map(t => (
              <div className="slip" key={t.id}>
                <div className="no">{t.号牌}</div>
                <div className="meta">{t.窗口} · {t.业务种类} · {t.状态}</div>
              </div>
            ))}
          </div>
        </section>
      )}

      <div className="wall-lanes">
        {wall.墙面.map(block => (
          <section
            className={`panel lane ${block.对齐?.灯 ? 'aligned' : 'unaligned'}`}
            key={block.种类编号}
          >
            <h2>
              {block.业务种类}
              <span className="tag">{block.前缀}</span>
              {!block.启用 && <span className="tag">停用</span>}
              {block.可叫
                ? <span className="tag">可叫 {block.可叫数}</span>
                : <span className="tag">禁叫</span>}
            </h2>
            <div className="lane-stat">
              等候 {block.等候数} · 过号 {block.过号数} · 真源行 {block.真源行数}
            </div>
            <div className={`align ${block.对齐?.灯 ? 'on' : 'off'}`}>
              <span className="lamp" aria-hidden="true" />
              <span>{block.对齐?.状态} · 灯{block.对齐?.灯 ? '亮' : '灭'}</span>
            </div>
            <p className="muted">{block.对齐?.说明}</p>
            <div className="card-grid">
              {block.卡片.length === 0 && <p className="muted">暂无号牌</p>}
              {block.卡片.map(t => (
                <div className={`slip ${t.状态 === '过号' ? 'skipped' : ''}`} key={t.id}>
                  <div className="no">{t.号牌}</div>
                  <div className="meta">{t.状态} · 位次 {t.位次}</div>
                </div>
              ))}
            </div>
          </section>
        ))}
        {wall.墙面.length === 0 && <p className="muted">当日暂无分栏</p>}
      </div>

      <section className="panel">
        <h2>位次物化</h2>
        <p className="muted">物化只是队列表的加速缓存；下表逐种类比对真源批次与物化批次。</p>
        <table className="table">
          <thead>
            <tr>
              <th>种类</th><th>真源行</th><th>物化卡</th><th>等候</th><th>过号</th><th>物化时刻</th><th>对齐</th>
            </tr>
          </thead>
          <tbody>
            {wall.物化.map(m => (
              <tr key={m.种类编号} className={m.一致 ? '' : 'bad'}>
                <td>{m.业务种类}</td>
                <td>{m.真源行数}</td>
                <td>{m.物化卡片数 ?? '—'}</td>
                <td>{m.等候数}</td>
                <td>{m.过号数}</td>
                <td className="muted">{m.物化时刻 || '—'}</td>
                <td>{m.一致 ? '一致' : m.状态}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h2>墙刷新审计</h2>
        <p className="muted">每个写口在同一写事务内落一条；对不上即记未对齐，绝不留半截。</p>
        {(wall.审计 || []).length === 0 && <p className="muted">暂无审计记录</p>}
        {(wall.审计 || []).length > 0 && (
          <table className="table">
            <thead>
              <tr><th>时刻</th><th>触发</th><th>种类</th><th>真源行</th><th>等候</th><th>过号</th><th>对齐</th><th>说明</th></tr>
            </thead>
            <tbody>
              {wall.审计.map(a => (
                <tr key={a.id} className={a.对齐 ? '' : 'bad'}>
                  <td className="muted">{a.时刻}</td>
                  <td>{a.触发}</td>
                  <td>{a.种类编号 ?? '—'}</td>
                  <td>{a.真源行数}</td>
                  <td>{a.等候数}</td>
                  <td>{a.过号数}</td>
                  <td>{a.对齐 ? '对齐' : '未对齐'}</td>
                  <td className="muted">{a.说明}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
