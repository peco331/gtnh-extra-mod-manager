"""GTNH 平台证据，与发布说明中的整合包小版本兼容提示分离。"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TargetDecision:
    status: str
    reason: str


def classify_target(file_name: str, *, release_tag: str = '',
                    target_profile: str = 'unknown') -> TargetDecision:
    versions = set()
    marked = False
    for text in (file_name.removesuffix('.jar'), release_tag):
        if re.search(r'(?i)(?:^|[-_.+ ])(?:fabric|neoforge|quilt)(?:$|[-_.+ ])', text):
            return TargetDecision('excluded', '该构建使用非 GTNH 的加载器')
        marked |= bool(re.search(r'(?i)(?:^|[-_.+ ])gtnh(?:$|[-_.+ ])', text))
        explicit = re.findall(r'(?i)(?:minecraft|mc)[-_ ]?(1\.\d+(?:\.\d+)?)(?![\d.])', text)
        versions.update(explicit)
        # 两段版本信息才把裸 MC 版本锚点作为证据，避免 Demo-1.20.1.jar 的误判。
        numbers = re.findall(r'(?i)(?<![\w.])v?(\d+(?:\.\d+)+)(?![\d.])', text)
        if not explicit and len(numbers) >= 2:
            # 常见 Name-MC-ModVersion：一旦首段为 MC 锚点，后段属于 Mod，
            # 不再次把 Mod 的 1.12.0 等版本号当成游戏平台。
            anchors = [v for v in numbers if re.fullmatch(r'1\.\d+(?:\.\d+)?', v)
                       and (v in ('1.6.4', '1.7.10') or int(v.split('.')[1]) >= 8)]
            if anchors:
                versions.add(anchors[0])
    if versions - {'1.7.10'}:
        return TargetDecision('excluded', 'Minecraft 版本不匹配：' + ', '.join(sorted(versions)))
    if versions or marked:
        return TargetDecision('eligible', '构建标识适用于 GTNH / Minecraft 1.7.10')
    if target_profile == 'gtnh':
        return TargetDecision('eligible', '用户已确认此下载源用于 GTNH')
    return TargetDecision('unknown', '未找到游戏平台标识，请确认此下载源用于 GTNH')
