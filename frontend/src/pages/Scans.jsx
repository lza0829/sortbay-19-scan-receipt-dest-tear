import React, { useEffect, useState } from 'react'
import { api } from '../api'

export default function Scans() {
  const [rows, setRows] = useState([])
  const [barcode, setBarcode] = useState('')
  const [dest, setDest] = useState('110')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [supMap, setSupMap] = useState({})
  const load = () => api('/扫描').then(setRows).catch((e) => setErr(e.message))
  useEffect(() => { load() }, [])

  const create = async () => {
    setErr(''); setMsg('')
    try {
      const rec = await api('/扫描', { method: 'POST', body: JSON.stringify({ 条码: barcode, 目的地码: dest }) })
      // 回执如实区分：缺目的地只挂待补码，绝不假装可裁决成功
      if (rec['装载结果'] === '待补码') {
        setMsg(`已挂账「待补目的地」：缺目的地码，不可裁决（编号 ${rec.id}）`)
      } else {
        setMsg(`过机装载成功，已进待路由桶（编号 ${rec.id}）`)
      }
      setBarcode(''); load()
    } catch (e) { setErr(e.message) }
  }

  const routeOne = async (id) => {
    setErr(''); setMsg('')
    try {
      await api(`/扫描/${id}/路由`, { method: 'POST' })
      setMsg('路由成功'); load()
    } catch (e) { setErr(e.message) }
  }

  const supplement = async (id) => {
    setErr(''); setMsg('')
    const code = (supMap[id] || '').trim()
    if (!code) { setErr('请填写补码目的地码'); return }
    try {
      const rec = await api(`/扫描/${id}/补码`, { method: 'POST', body: JSON.stringify({ 目的地码: code }) })
      setMsg(`补码成功：${rec.barcode} -> ${rec.dest_code}，已可裁决`)
      setSupMap((m) => ({ ...m, [id]: '' })); load()
    } catch (e) { setErr(e.message) }
  }

  return (
    <div className="panel">
      <h2>过机扫描</h2>
      <div className="row">
        <label>条码<input value={barcode} onChange={(e) => setBarcode(e.target.value)} /></label>
        <label>目的地码（可空，空则挂待补码）<input value={dest} onChange={(e) => setDest(e.target.value)} /></label>
        <button type="button" onClick={create}>装载扫描</button>
      </div>
      {msg && <div className="msg">{msg}</div>}
      {err && <div className="err">{err}</div>}
      <table>
        <thead>
          <tr><th>编号</th><th>条码</th><th>目的地</th><th>状态</th><th>可裁决/拒因</th><th>口门</th><th>操作</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const needDest = r.status === '待补目的地'
            return (
              <tr key={r.id} className={needDest ? 'row-warn' : ''}>
                <td>{r.id}</td>
                <td>{r.barcode}</td>
                <td>{r.dest_code || <span className="tag tag-warn">缺码</span>}</td>
                <td>{r.status}</td>
                <td>
                  {r['可裁决']
                    ? <span className="tag tag-ok">可裁决</span>
                    : <span className="tag tag-danger" title={r.reject_reason}>{r['拒因'] || r.reject_reason || '不可裁决'}</span>}
                </td>
                <td>{r.chute_id || '-'}</td>
                <td>
                  {needDest && (
                    <span className="inline-sup">
                      <input
                        placeholder="补目的地码"
                        value={supMap[r.id] || ''}
                        onChange={(e) => setSupMap((m) => ({ ...m, [r.id]: e.target.value }))}
                      />
                      <button type="button" className="secondary" onClick={() => supplement(r.id)}>补码</button>
                    </span>
                  )}
                  {/* 裁决入口仅对可裁决件可用，缺码/待补码件置灰，不假装可落入 */}
                  {(r.status === '待路由' || r.status === '错分回流') && (
                    r['可裁决']
                      ? <button type="button" onClick={() => routeOne(r.id)}>路由</button>
                      : <button type="button" disabled title={r['拒因'] || '缺目的地码'}>路由</button>
                  )}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
