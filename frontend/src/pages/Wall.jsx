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
        <p className="muted">
          重放批次 {wall.重放批次?.slice(0, 12)}… · 分栏 {wall.对齐?.分栏数 ?? 0}
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
    </div>
  )
}
