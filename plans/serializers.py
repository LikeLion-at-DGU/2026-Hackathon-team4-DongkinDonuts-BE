from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from context.models import NextActivityPlan, UserContextSnapshot
from routines.serializers import ActivityTypeSerializer
from sessions_app.models import DifficultyFeedback, RecoveryFeeling, SessionFeedback

from .models import (
    AIInsight,
    InsightType,
    Notification,
    NotificationKind,
    NotificationStatus,
    RecoveryPlan,
    RecoverySlot,
    SlotStatus,
    WebPushSubscription,
)


AI_SOURCE_LABELS = {
    "context_snapshot": "사용자 입력 정보",
    "next_activity_plan": "집중 예정 시간",
    "pc_usage_patterns": "디지털 사용 패턴",
    "pc_usage_analysis": "디지털 패턴 분석",
    "time_policy": "회복 타이머 정책",
    "previous_sessions": "이전 Brainfit 기록",
    "previous_feedback": "이전 피드백",
    "previous_state_frequencies": "이전 상태 기록",
    "activity_catalog": "Brainfit 루틴 목록",
    "llm_summary": "AI 분석 요약",
    "llm_slot_reason": "추천 시간 설명",
    "llm_routine_reason": "추천 루틴 설명",
}

HISTORY_STATUS_LABELS = {
    "COMPLETED": "완료",
    "UPCOMING": "진행 예정",
    "MISSED": "미완료",
    "IN_PROGRESS": "진행중",
    "CANCELED": "취소",
}

NOTIFICATION_RESPONSE_GRACE_MINUTES = 10


def unique_list(values):
    seen = set()
    unique = []
    for value in values:
        if value and value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def source_labels(source_keys):
    return unique_list(AI_SOURCE_LABELS.get(source, source) for source in source_keys)


def plan_input_snapshot(plan):
    ai_run = getattr(plan, "ai_plan_run", None)
    if ai_run is None:
        return {}
    snapshot = getattr(ai_run, "input_snapshot_json", None)
    return snapshot if isinstance(snapshot, dict) else {}


def data_source_summary_for_plan(plan):
    generation_snapshot = plan.generation_snapshot_json or {}
    input_snapshot = plan_input_snapshot(plan)

    used_user_context = bool(
        generation_snapshot.get("context_snapshot")
        or input_snapshot.get("context_snapshot")
    )
    used_next_activity_plan = bool(
        generation_snapshot.get("next_activity_plan")
        or input_snapshot.get("next_activity_plan")
    )
    pc_usage_patterns = input_snapshot.get("pc_usage_patterns") or generation_snapshot.get("pc_usage_patterns") or []
    used_digital_patterns = bool(
        generation_snapshot.get("has_pc_usage_pattern")
        or generation_snapshot.get("has_today_pc_usage_pattern")
        or pc_usage_patterns
    )
    used_history = bool(
        input_snapshot.get("previous_sessions")
        or input_snapshot.get("previous_feedback")
        or input_snapshot.get("previous_state_frequencies")
    )
    pc_usage_pattern_count = generation_snapshot.get("pc_usage_pattern_count", len(pc_usage_patterns))

    labels = []
    if used_user_context:
        labels.append("사용자 입력 정보")
    if used_next_activity_plan:
        labels.append("집중 예정 시간")
    if used_digital_patterns:
        labels.append("디지털 사용 패턴")
    if used_history:
        labels.append("이전 Brainfit 기록")

    return {
        "labels": labels,
        "used_user_context": used_user_context,
        "used_next_activity_plan": used_next_activity_plan,
        "used_digital_patterns": used_digital_patterns,
        "used_history": used_history,
        "pc_usage_pattern_count": pc_usage_pattern_count,
    }


def state_option_items(context_snapshot):
    if context_snapshot is None:
        return []
    return [
        {
            "code": link.state_id,
            "label": link.state.label,
            "priority": link.priority,
        }
        for link in context_snapshot.state_links.all()
    ]


def activity_tag_items(next_activity_plan):
    if next_activity_plan is None:
        return []
    return [
        {
            "code": link.activity_tag_id,
            "name": link.activity_tag.name,
            "category": link.activity_tag.category,
        }
        for link in next_activity_plan.activity_tag_links.all()
    ]


def history_status_for_slot(slot, now):
    now = now or timezone.now()
    if slot.status == SlotStatus.COMPLETED:
        return "COMPLETED"
    if slot.status == SlotStatus.CANCELED:
        return "CANCELED"
    if slot.status == SlotStatus.STARTED:
        return "IN_PROGRESS"
    if _has_expired_sent_recovery_notification(slot, now):
        return "CANCELED"
    if slot.effective_time and slot.effective_time < now:
        return "MISSED"

    return "UPCOMING"


def _has_expired_sent_recovery_notification(slot, now):
    for notification in slot.notifications.all():
        if (
            notification.kind != NotificationKind.RECOVERY_SLOT
            or notification.status != NotificationStatus.SENT
        ):
            continue
        sent_at = notification.sent_at or notification.scheduled_at
        if sent_at and sent_at + timedelta(minutes=NOTIFICATION_RESPONSE_GRACE_MINUTES) <= now:
            return True
    return False


def slot_input_summary(slot):
    states = state_option_items(slot.context_snapshot)
    activity_tags = activity_tag_items(slot.next_activity_plan)

    parts = []
    if states:
        parts.append("현재 상태: " + ", ".join(state["label"] for state in states))
    if slot.context_snapshot and slot.context_snapshot.note:
        parts.append(f"메모: {slot.context_snapshot.note}")

    activity_parts = []
    if activity_tags:
        activity_parts.append(", ".join(tag["name"] for tag in activity_tags))
    if slot.next_activity_plan and slot.next_activity_plan.expected_activity_minutes:
        activity_parts.append(f"{slot.next_activity_plan.expected_activity_minutes}분 예정")
    if activity_parts:
        parts.append("이후 활동: " + " / ".join(activity_parts))

    return " · ".join(parts)


class AIInsightSerializer(serializers.ModelSerializer):
    data_source_labels = serializers.SerializerMethodField()

    def get_data_source_labels(self, obj):
        return source_labels(obj.data_sources_json or [])

    class Meta:
        model = AIInsight
        fields = [
            "id",
            "recovery_plan",
            "recovery_slot",
            "routine_instance",
            "insight_type",
            "body",
            "data_sources_json",
            "data_source_labels",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class RecoverySlotRoutineInstanceSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    activity = ActivityTypeSerializer()
    stage_type = serializers.CharField(source="activity.stage_type")
    sequence_no = serializers.IntegerField()
    difficulty_level = serializers.IntegerField()
    planned_duration_sec = serializers.IntegerField()
    status = serializers.CharField()
    locked_until_previous_done = serializers.BooleanField()
    completed_at = serializers.DateTimeField(allow_null=True)
    insights = serializers.SerializerMethodField()

    def get_insights(self, obj):
        return AIInsightSerializer(obj.insights.all(), many=True).data


class SlotFeedbackSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionFeedback
        fields = [
            "id",
            "recovery_feeling",
            "difficulty_feedback",
            "skipped",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class SlotFeedbackSubmitSerializer(serializers.Serializer):
    recovery_feeling = serializers.ChoiceField(choices=RecoveryFeeling.choices)
    difficulty_feedback = serializers.ChoiceField(choices=DifficultyFeedback.choices)


class NotificationSerializer(serializers.ModelSerializer):
    recovery_slot_effective_time = serializers.SerializerMethodField()

    def get_recovery_slot_effective_time(self, obj):
        if obj.recovery_slot_id is None:
            return None
        return obj.recovery_slot.effective_time

    class Meta:
        model = Notification
        fields = [
            "id",
            "recovery_slot",
            "recovery_slot_effective_time",
            "kind",
            "message",
            "scheduled_at",
            "sent_at",
            "clicked_at",
            "status",
            "data_json",
            "delivery_error",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class RecoverySlotSerializer(serializers.ModelSerializer):
    effective_time = serializers.DateTimeField(read_only=True)
    routine_instances = RecoverySlotRoutineInstanceSerializer(many=True, read_only=True)
    feedback = SlotFeedbackSerializer(read_only=True)
    notifications = NotificationSerializer(many=True, read_only=True)
    insights = serializers.SerializerMethodField()
    data_source_summary = serializers.SerializerMethodField()

    def get_insights(self, obj):
        insights = obj.insights.filter(routine_instance__isnull=True)
        return AIInsightSerializer(insights, many=True).data

    def get_data_source_summary(self, obj):
        return data_source_summary_for_plan(obj.recovery_plan)

    class Meta:
        model = RecoverySlot
        fields = [
            "id",
            "recovery_plan",
            "ai_plan_run",
            "context_snapshot",
            "next_activity_plan",
            "sequence_no",
            "recommended_at",
            "scheduled_at",
            "user_changed_at",
            "effective_time",
            "interval_minutes",
            "repeat_rule",
            "notification_enabled",
            "notification_basis",
            "status",
            "insights",
            "data_source_summary",
            "routine_instances",
            "feedback",
            "notifications",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "recovery_plan",
            "ai_plan_run",
            "sequence_no",
            "effective_time",
            "created_at",
            "updated_at",
        ]


class RecoveryPlanSerializer(serializers.ModelSerializer):
    slots = RecoverySlotSerializer(many=True, read_only=True)
    insights = serializers.SerializerMethodField()
    data_source_summary = serializers.SerializerMethodField()

    def get_insights(self, obj):
        insights = obj.insights.filter(recovery_slot__isnull=True, routine_instance__isnull=True)
        return AIInsightSerializer(insights, many=True).data

    def get_data_source_summary(self, obj):
        return data_source_summary_for_plan(obj)

    class Meta:
        model = RecoveryPlan
        fields = [
            "id",
            "ai_plan_run",
            "plan_date",
            "generation_snapshot_json",
            "status",
            "insights",
            "data_source_summary",
            "slots",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class RecoverySlotHistorySerializer(RecoverySlotSerializer):
    plan_date = serializers.DateField(source="recovery_plan.plan_date", read_only=True)
    start_time = serializers.SerializerMethodField()
    history_status = serializers.SerializerMethodField()
    history_status_label = serializers.SerializerMethodField()
    context_snapshot_detail = serializers.SerializerMethodField()
    next_activity_plan_detail = serializers.SerializerMethodField()
    input_summary = serializers.SerializerMethodField()
    recommended_routines = serializers.SerializerMethodField()
    remark = serializers.SerializerMethodField()
    data_notice = serializers.SerializerMethodField()

    def get_start_time(self, obj):
        return obj.effective_time.strftime("%H:%M") if obj.effective_time else None

    def get_history_status(self, obj):
        now = self.context.get("now")
        return history_status_for_slot(obj, now)

    def get_history_status_label(self, obj):
        return HISTORY_STATUS_LABELS[self.get_history_status(obj)]

    def get_context_snapshot_detail(self, obj):
        if obj.context_snapshot is None:
            return None
        return {
            "id": obj.context_snapshot.id,
            "service_date": obj.context_snapshot.service_date,
            "note": obj.context_snapshot.note,
            "state_options": state_option_items(obj.context_snapshot),
            "created_at": obj.context_snapshot.created_at,
        }

    def get_next_activity_plan_detail(self, obj):
        if obj.next_activity_plan is None:
            return None
        return {
            "id": obj.next_activity_plan.id,
            "service_date": obj.next_activity_plan.service_date,
            "expected_activity_minutes": obj.next_activity_plan.expected_activity_minutes,
            "activity_tags": activity_tag_items(obj.next_activity_plan),
            "created_at": obj.next_activity_plan.created_at,
        }

    def get_input_summary(self, obj):
        return slot_input_summary(obj)

    def get_recommended_routines(self, obj):
        routines = []
        for routine in obj.routine_instances.all():
            reason = None
            for insight in routine.insights.all():
                if insight.insight_type == InsightType.ROUTINE_REASON:
                    reason = insight.body
                    break
            routines.append(
                {
                    "id": routine.id,
                    "sequence_no": routine.sequence_no,
                    "stage_type": routine.activity.stage_type,
                    "activity": ActivityTypeSerializer(routine.activity).data,
                    "difficulty_level": routine.difficulty_level,
                    "planned_duration_sec": routine.planned_duration_sec,
                    "status": routine.status,
                    "recommended_at": obj.effective_time,
                    "recommended_time": self.get_start_time(obj),
                    "reason": reason,
                }
            )
        return routines

    def get_remark(self, obj):
        """
        Your History 비고(remark) 문구 결정 로직:
        - 취소 (CANCELED) -> "진행 예정 취소"
        - 진행중 (IN_PROGRESS) / 완료 (COMPLETED) -> "" (비워두기)
        - 진행 예정 (UPCOMING):
          * 트랙 1 (PC 사용 패턴 분석 기반 고정 알림) -> "brainfit의 추천 시간"
          * 트랙 2 (사용자가 세션 후 입력한 타이머 수동 예약) -> "타이머 예약 시간"
        """

        status = self.get_history_status(obj)

        if status == "CANCELED":
            return "진행 예정 취소"

        if status in ["COMPLETED", "IN_PROGRESS"]:
            return ""

        if status == "MISSED":
            for insight in obj.insights.all():
                if insight.insight_type == InsightType.RECOMMENDATION_REASON:
                    return insight.body
            return ""

        if status == "UPCOMING":
            # 트랙 2: 사용자가 직접 시간을 변경(user_changed_at)했거나 수동 지정(scheduled_at)한 타이머
            if obj.user_changed_at is not None or (
                obj.scheduled_at is not None and obj.scheduled_at != obj.recommended_at
            ):
                return "타이머 예약 시간"

            # 트랙 1: PC 패턴 분석으로 생성된 기본 추천 시각 (recommended_at)
            return "brainfit의 추천 시간"

        return ""

    def get_data_notice(self, obj):
        labels = self.get_data_source_summary(obj)["labels"]
        if not labels:
            return ""
        return f"{', '.join(labels)}를 기반으로 생성된 기록입니다."

    class Meta(RecoverySlotSerializer.Meta):
        fields = RecoverySlotSerializer.Meta.fields + [
            "plan_date",
            "start_time",
            "history_status",
            "history_status_label",
            "context_snapshot_detail",
            "next_activity_plan_detail",
            "input_summary",
            "recommended_routines",
            "remark",
            "data_notice",
        ]


class RecoverySlotHistoryQuerySerializer(serializers.Serializer):
    date = serializers.DateField(required=False)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)

    def validate(self, attrs):
        if attrs.get("date") and (attrs.get("start_date") or attrs.get("end_date")):
            raise serializers.ValidationError("date와 start_date/end_date는 함께 사용할 수 없습니다.")

        start_date = attrs.get("start_date")
        end_date = attrs.get("end_date")
        if start_date and end_date and start_date > end_date:
            raise serializers.ValidationError("start_date는 end_date보다 늦을 수 없습니다.")
        return attrs


class OwnedContextInputMixin:
    context_snapshot = serializers.PrimaryKeyRelatedField(
        queryset=UserContextSnapshot.objects.all(),
        required=False,
        allow_null=True,
        default=None,
    )
    next_activity_plan = serializers.PrimaryKeyRelatedField(
        queryset=NextActivityPlan.objects.all(),
        required=False,
        allow_null=True,
        default=None,
    )

    def validate_context_snapshot(self, value):
        if value is not None and value.user_id != self.context["request"].user.id:
            raise serializers.ValidationError("본인의 상태 스냅샷만 사용할 수 있습니다.")
        return value

    def validate_next_activity_plan(self, value):
        if value is not None and value.user_id != self.context["request"].user.id:
            raise serializers.ValidationError("본인의 이후 활동 계획만 사용할 수 있습니다.")
        return value


class RecoveryPlanCreateSerializer(OwnedContextInputMixin, serializers.Serializer):
    recommended_times = serializers.ListField(
        child=serializers.DateTimeField(),
        required=False,
        allow_empty=True,
        default=list,
    )
    notification_enabled = serializers.BooleanField(default=True)


class AIRecoveryPlanGenerateSerializer(OwnedContextInputMixin, serializers.Serializer):
    notification_enabled = serializers.BooleanField(default=True)


class RecoverySlotCreateSerializer(OwnedContextInputMixin, serializers.Serializer):
    recommended_at = serializers.DateTimeField(required=False, allow_null=True, default=None)
    notification_enabled = serializers.BooleanField(default=True)


class RecoverySlotScheduleSerializer(serializers.Serializer):
    scheduled_at = serializers.DateTimeField()


class RecoverySlotCancelBeforeSerializer(serializers.Serializer):
    before = serializers.DateTimeField()
    exclude_slot = serializers.PrimaryKeyRelatedField(
        queryset=RecoverySlot.objects.all(),
        required=False,
        allow_null=True,
        default=None,
    )

    def validate_exclude_slot(self, value):
        if value is not None and value.recovery_plan.user_id != self.context["request"].user.id:
            raise serializers.ValidationError("본인의 회복 슬롯만 제외할 수 있습니다.")
        return value


class RecoverySlotNotificationSerializer(serializers.Serializer):
    notification_enabled = serializers.BooleanField()
    repeat_rule = serializers.CharField(required=False, allow_blank=True, default="")


class WebPushSubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebPushSubscription
        fields = [
            "id",
            "endpoint",
            "user_agent",
            "is_active",
            "last_seen_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


class WebPushSubscriptionCreateSerializer(serializers.Serializer):
    endpoint = serializers.CharField()
    keys = serializers.DictField()
    user_agent = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_keys(self, value):
        p256dh = value.get("p256dh")
        auth = value.get("auth")
        if not p256dh or not auth:
            raise serializers.ValidationError("keys.p256dh와 keys.auth가 필요합니다.")
        return value
