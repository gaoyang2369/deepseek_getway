import {
  node,
  money,
  date,
  notice,
  confirmAction,
  api,
  button,
  badge,
  table,
  toolbar,
  field,
  yuan,
} from "./common.js";
import { openForm, closeModal } from "./forms.js";
let includePriceHistory = false;
export async function pricesView(cache, load) {
  const rows = await api(
    "/prices" + (includePriceHistory ? "?include_ended=true" : ""),
  );
  const source = node("a", "查看 DeepSeek 官方人民币价格");
  source.href = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/";
  source.target = "_blank";
  source.rel = "noopener noreferrer";
  return [
    node(
      "p",
      "直接从 DeepSeek 官网导入模型与人民币价格，无需手动填写。导入值按官方高峰价保守估算，空闲时段实际费用可能更低；明细中保留空闲价、来源和核对日期。更新不改变历史费用。",
      "section-note",
    ),
    source,
    toolbar(
      includePriceHistory ? "全部计价版本（含历史）" : "当前可用模型",
      button("从 DeepSeek 官网更新", async () => {
        const result = await api("/prices/import-official", "POST", {});
        await load();
        notice(
          "已从 DeepSeek 官网获取价格：更新 " +
            result.created.length +
            " 个模型，" +
            result.unchanged.length +
            " 个无需变更。按官方高峰价保守估算。",
          "success",
        );
      }),
      button(
        includePriceHistory ? "仅看当前模型" : "查看历史计价",
        async () => {
          includePriceHistory = !includePriceHistory;
          await load();
        },
      ),
      button("＋ 自定义价格", () =>
        openForm(
          "发布计价版本",
          [
            field("客户端模型名", "model"),
            field("DeepSeek 上游模型名", "upstream_model"),
            field("唯一版本号", "version"),
            field("缓存未命中：元 / 百万 Token", "input_miss", "number"),
            field("缓存命中：元 / 百万 Token", "input_hit", "number"),
            field("输出：元 / 百万 Token", "output", "number"),
            field("费用标记", "estimated", "text", "true", [
              ["true", "估算"],
              ["false", "经核验的固定精确价"],
            ]),
            field("来源、计价时段、币种换算及保守规则说明", "note", "textarea"),
          ],
          async (d) => {
            await api("/prices", "POST", {
              ...d,
              input_miss: yuan(d.input_miss),
              input_hit: yuan(d.input_hit),
              output: yuan(d.output),
              estimated: d.estimated === "true",
            });
            closeModal();
            await load();
          },
        ),
      ),
    ),
    table(
      [
        "模型",
        "上游模型",
        "版本",
        "未命中 / 命中 / 输出（元/百万）",
        "标记",
        "生效",
        "结束",
        "依据",
        "操作",
      ],
      rows.map((r) => [
        r.model,
        r.upstream_model,
        r.version,
        [r.input_miss, r.input_hit, r.output].map(money).join(" / "),
        r.estimated ? "估算" : "已核验",
        date(r.starts_at),
        date(r.ends_at),
        r.note,
        r.ends_at && new Date(r.ends_at) <= new Date()
          ? badge("已结束")
          : button(
              "删除模型",
              async () => {
                if (
                  !(await confirmAction(
                    "删除模型“" + r.model + "”？",
                    "该计价版本将停止供新请求使用；没有其他有效计价时，该模型将从可用列表移除。历史调用和账本保留。",
                  ))
                )
                  return;
                await api("/prices/" + r.id, "DELETE");
                await load();
                notice("模型已移出当前列表，历史计价和账本已保留。", "success");
              },
              "danger",
            ),
      ]),
    ),
  ];
}
