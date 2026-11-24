import { useEffect, useState } from 'react'
import { api } from '../api'

const surface_panel = true
export default function Take() {
  const [kinds, setKinds] = useState([])
  const [kid, setKid] = useState('')
  const [slip, setSlip] = useState(null)
  const [check, setCheck] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api.kinds().then(ks => {
    setKinds(ks.filter(k => k.启用))
    if (!kid && ks.length) setKid(String(ks.find(k => k.启用)?.id || ''))
  }).catch(e => setErr(e.message))
  useEffect(() => { load() }, [])
  const onTake = async () => {
    setErr('')
    try {
      const t = await api.take(Number(kid))
      setSlip(t)
      // 回执后立刻按同一真源取墙与总览，确认三者已同刷。
      const [w, o] = await Promise.all([api.wall(), api.overview()])
      const lane = w.墙面.find(b => b.业务种类 === t.业务种类)
      const onWall = lane?.卡片.some(c => c.id === t.id) ?? false
      const probe = o.墙面核对
      setCheck({
        墙上在位: onWall,
        位次: lane?.卡片.find(c => c.id === t.id)?.位次,
        对齐灯: w.对齐?.灯,
        可叫: w.可叫,
        高亮一致: w.高亮核对?.一致,
        同批: probe?.重放批次 === w.重放批次,
        等候总数: o.等候,
      })
      load()
    } catch (e) { setErr(e.message) }
  }
  return (
    <div className={surface_panel ? "page" : "page"}>
      <header className="hero">
        <p className="eyebrow">发号入队</p>
        <h1>取号</h1>
        <p className="lede">按业务种类发放当日号牌，写入等候队列。</p>
      </header>
      <section className="panel">
        <div className="form row">
          <label>业务种类
            <select value={kid} onChange={e => setKid(e.target.value)}>
              {kinds.map(k => <option key={k.id} value={k.id}>{k.名称}（{k.前缀}）</option>)}
            </select>
          </label>
          <button type="button" onClick={onTake} disabled={!kid}>取号</button>
        </div>
        {err && <p className="err">{err}</p>}
        {slip && (
          <div className="slip" style={{ marginTop: 16 }}>
            <div className="no">{slip.号牌}</div>
            <div className="meta">{slip.业务种类} · {slip.状态} · 位次 {slip.位次}</div>
          </div>
        )}
        {check && (
          <p className={check.墙上在位 && check.同批 ? 'muted' : 'err'}>
            取号回执核验：墙面{check.墙上在位 ? '已在位' : '未在位'} · 墙面位次 {check.位次 ?? '—'} ·
            对齐灯{check.对齐灯 ? '亮' : '灭'} · 高亮{check.高亮一致 ? '一致' : '拧花'} ·
            可叫{check.可叫 ? '是' : '否'} · 与总览{check.同批 ? '同批' : '不同批（半刷）'} · 总览等候 {check.等候总数}
          </p>
        )}
      </section>
    </div>
  )
}
