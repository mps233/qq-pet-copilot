import type { Data } from '../api'
import { hrs, pct } from '../lib/format'

/** 顶部两颗数据胶囊（官方结构：图标在胶囊外面且比胶囊大，文字在胶囊内居中）。
 *  第一排：金币 / 今日踩踩 / 今日PK；第二排：今日冒险 / 学习+打工合计 / 经验日常。
 *  与 legacy 的 renderData 逐项对齐（含分母取哪个值）。 */
export function Capsules({ data }: { data: Data }) {
  const st = data.status
  const pg = data.progress || {}
  const cfg = data.config || {}

  const visit = pg.visit?.learned ?? null
  const visitMax = cfg.visit_per_day || 10
  const pk = pg.pk?.learned ?? null
  const pkMax = cfg.pk_per_day || 15
  const adv = pg.adventure?.learned ?? 0
  const advMax = cfg.adventure_times || 1

  // 学习+打工合计：分母取当天的「合计停止点」——editable 里四个值取 >0 的最大值，
  // 都没配才退回 study_quota + work_quota（拿配额当分母会在 24+24 时永远空空如也）。
  const schoolSecs = pg.school?.study_secs ?? 0
  const workSecs = pg.work?.work_secs ?? 0
  const totalHrs = Number(hrs(schoolSecs + workSecs))
  const edt = data.editable || {}
  const stopPts = [
    edt.stop_total_hours,
    edt.efficiency_tier2_hours,
    edt.daily_hour_limit,
    edt.work_stop_hours,
  ]
    .map(Number)
    .filter((v) => v > 0)
  const targetHrs = stopPts.length
    ? Math.max(...stopPts)
    : (Number(cfg.study_quota_hours) || 0) + (Number(cfg.work_quota_hours) || 0)

  const swTip =
    `今日学习+打工合计 ${totalHrs} 小时` +
    (targetHrs ? `（目标 ${targetHrs}h = 合计停止点）` : '') +
    ` · 学习 ${pg.school?.learned ?? 0} 节 ${hrs(schoolSecs)}h` +
    ` / 打工 ${pg.work?.learned ?? 0} 次 ${hrs(workSecs)}h`

  const expDone = pg.exp_daily?.done

  return (
    <div className="flt caps">
      <div className="caps-row">
        <div className="cap" title="金币">
          <img className="cico" src="/qp-icons/official/cap_coin.png" alt="" />
          <div className="capbody">
            <span className="cval" id="coins">
              {st.coins != null ? st.coins : '--'}
            </span>
            <span className="cunit" id="coinsAt">
              {st.coins != null && st.updated ? `· ${st.updated.slice(11, 16)}` : ''}
            </span>
          </div>
        </div>

        <div className="cap" title="今日踩踩">
          <img className="cico" src="/qp-icons/official/cap_paw.png" alt="" />
          <div className="capbody">
            <span className="cval" id="visitTxt">
              {visit != null ? visit : '--'}/{visitMax}
            </span>
            <div className="bar">
              <i id="visitBar" style={{ width: `${visit != null ? pct(visit, visitMax) : 0}%` }} />
            </div>
          </div>
        </div>

        <div className="cap" title="今日PK">
          <img className="cico" src="/qp-icons/official/pk_words.png" alt="" />
          <div className="capbody">
            <span className="cval" id="pkTxt">
              {pk != null ? pk : '--'}/{pkMax}
            </span>
            <div className="bar">
              <i id="pkBar" style={{ width: `${pk != null ? pct(pk, pkMax) : 0}%` }} />
            </div>
          </div>
        </div>
      </div>

      <div className="caps-row">
        <div className="cap" title="今日冒险">
          <img className="cico" src="/qp-icons/official/cap_compass.png" alt="" />
          <div className="capbody">
            <span className="cval" id="advTxt">
              {adv}/{advMax}
            </span>
          </div>
        </div>

        <div className="cap" id="capSw" title={swTip}>
          <img className="cico" src="/qp-icons/official/cap_cookie.png" alt="" />
          <div className="capbody">
            <span className="cval" id="swTxt" title={swTip}>
              {totalHrs}h{targetHrs ? `/${targetHrs}h` : ''}
            </span>
            <div className="bar">
              <i
                id="swBarTotal"
                style={{ width: `${targetHrs ? pct(totalHrs, targetHrs) : 0}%` }}
              />
            </div>
          </div>
        </div>

        <div className="cap" title="经验日常">
          <img className="cico" src="/qp-icons/official/cap_diamond.png" alt="" />
          <div className="capbody">
            <span className="cval" id="expTxt">
              {expDone ? '✓ 完成' : '未完成'}
            </span>
          </div>
        </div>
      </div>
    </div>
  )
}
