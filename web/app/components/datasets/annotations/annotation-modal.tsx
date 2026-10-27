'use client'
import type { FC } from 'react'
import { Button } from '@langgenius/dify-ui/button'
import {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogHeaderCloseButton,
  DialogTitle,
} from '@langgenius/dify-ui/dialog'
import { Input } from '@langgenius/dify-ui/input'
import {
  Select,
  SelectItem,
  SelectItemIndicator,
  SelectItemText,
  SelectList,
  SelectPopup,
  SelectPortal,
  SelectPositioner,
  SelectTrigger,
  SelectValue,
} from '@langgenius/dify-ui/select'
import { Textarea } from '@langgenius/dify-ui/textarea'
import { useMutation } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import TagInput from '@/app/components/base/tag-input'
import { toast } from '@/app/notifications'
import {
  createDatasetAnnotation,
  type DatasetAnnotation,
  type DatasetAnnotationFeedbackType,
  updateDatasetAnnotation,
} from '@/service/dataset-annotation'

type AnnotationModalProps = {
  isShow: boolean
  onHide: () => void
  datasetId: string
  annotation?: DatasetAnnotation | null
  defaultQuestion?: string
  defaultAnswer?: string
  onSaved: (annotation: DatasetAnnotation) => void
}

const FEEDBACK_OPTIONS = [
  { value: 'none', labelKey: 'feedbackType.none' },
  { value: 'like', labelKey: 'feedbackType.like' },
  { value: 'dislike', labelKey: 'feedbackType.dislike' },
] as const

const AnnotationModal: FC<AnnotationModalProps> = ({
  isShow,
  onHide,
  datasetId,
  annotation,
  defaultQuestion = '',
  defaultAnswer = '',
  onSaved,
}) => {
  const { t } = useTranslation(['datasetAnnotations', 'common'])
  const isEdit = !!annotation
  const [question, setQuestion] = useState(defaultQuestion)
  const [answer, setAnswer] = useState(defaultAnswer)
  const [tags, setTags] = useState<string[]>([])
  const [feedbackType, setFeedbackType] = useState<string>('none')

  useEffect(() => {
    if (!isShow) return
    setQuestion(annotation?.question ?? defaultQuestion)
    setAnswer(annotation?.answer ?? defaultAnswer)
    setTags(annotation?.tags ?? [])
    setFeedbackType(annotation?.feedback_type ?? 'none')
  }, [isShow, annotation, defaultQuestion, defaultAnswer])

  const feedbackTypeValue = useMemo(
    () => (FEEDBACK_OPTIONS.some((option) => option.value === feedbackType) ? feedbackType : 'none'),
    [feedbackType],
  )

  const { mutateAsync: saveAnnotation, isPending } = useMutation({
    mutationFn: () => {
      const payload = {
        question,
        answer,
        tags,
        feedback_type:
          feedbackTypeValue === 'none' ? null : (feedbackTypeValue as DatasetAnnotationFeedbackType),
      }
      return isEdit
        ? updateDatasetAnnotation(datasetId, annotation!.id, payload)
        : createDatasetAnnotation(datasetId, payload)
    },
    onSuccess: (saved) => {
      toast.success(t(($) => $['api.actionSuccess'], { ns: 'common' }))
      onSaved(saved)
      onHide()
    },
    onError: () => {
      toast.error(t(($) => $['api.actionFailed'], { ns: 'common' }))
    },
  })

  const handleSubmit = () => {
    if (!question.trim() || !answer.trim()) {
      toast.error(t(($) => $['form.required'], { ns: 'datasetAnnotations' }))
      return
    }
    void saveAnnotation()
  }

  return (
    <Dialog open={isShow} onOpenChange={(open) => !open && !isPending && onHide()}>
      <DialogContent className="w-130 max-w-[calc(100vw-2rem)]">
        <DialogHeader>
          <DialogTitle>
            {isEdit
              ? t(($) => $['edit.title'], { ns: 'datasetAnnotations' })
              : t(($) => $['create.title'], { ns: 'datasetAnnotations' })}
          </DialogTitle>
          <DialogHeaderCloseButton
            aria-label={t(($) => $['operation.close'], { ns: 'common' })}
            disabled={isPending}
          />
        </DialogHeader>
        <DialogBody>
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <label htmlFor="annotation-question" className="system-sm-semibold text-text-secondary">
                {t(($) => $['form.question'], { ns: 'datasetAnnotations' })}
              </label>
              <Input
                id="annotation-question"
                value={question}
                onValueChange={setQuestion}
                disabled={isPending}
                placeholder={t(($) => $['form.questionPlaceholder'], { ns: 'datasetAnnotations' })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="annotation-answer" className="system-sm-semibold text-text-secondary">
                {t(($) => $['form.answer'], { ns: 'datasetAnnotations' })}
              </label>
              <Textarea
                id="annotation-answer"
                value={answer}
                onValueChange={setAnswer}
                readOnly={isPending}
                rows={6}
                placeholder={t(($) => $['form.answerPlaceholder'], { ns: 'datasetAnnotations' })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label htmlFor="annotation-tags" className="system-sm-semibold text-text-secondary">
                {t(($) => $['form.tags'], { ns: 'datasetAnnotations' })}
              </label>
              <TagInput
                id="annotation-tags"
                items={tags}
                onChange={setTags}
                disableAdd={isPending}
                placeholder={t(($) => $['form.tagsPlaceholder'], { ns: 'datasetAnnotations' })}
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <label className="system-sm-semibold text-text-secondary">
                {t(($) => $['form.feedbackType'], { ns: 'datasetAnnotations' })}
              </label>
              <Select<string>
                value={feedbackTypeValue}
                disabled={isPending}
                onValueChange={(nextValue) => setFeedbackType(nextValue ?? 'none')}
              >
                <SelectTrigger className="w-fit min-w-40">
                  <SelectValue>
                    {t(
                      ($) =>
                        $[
                          FEEDBACK_OPTIONS.find((option) => option.value === feedbackTypeValue)
                            ?.labelKey ?? 'feedbackType.none'
                        ],
                      { ns: 'datasetAnnotations' },
                    )}
                  </SelectValue>
                </SelectTrigger>
                <SelectPortal>
                  <SelectPositioner>
                    <SelectPopup>
                      <SelectList className="p-1">
                        {FEEDBACK_OPTIONS.map((option) => (
                          <SelectItem
                            key={option.value}
                            value={option.value}
                            className="relative h-8 py-0 pr-2 pl-7 system-md-regular"
                          >
                            <SelectItemText>
                              {t(($) => $[option.labelKey], { ns: 'datasetAnnotations' })}
                            </SelectItemText>
                            <SelectItemIndicator className="absolute top-2 left-2 ml-0" />
                          </SelectItem>
                        ))}
                      </SelectList>
                    </SelectPopup>
                  </SelectPositioner>
                </SelectPortal>
              </Select>
            </div>
          </div>
        </DialogBody>
        <DialogFooter>
          <DialogClose disabled={isPending} render={<Button />}>
            {t(($) => $['operation.cancel'], { ns: 'common' })}
          </DialogClose>
          <Button variant="primary" loading={isPending} onClick={handleSubmit}>
            {t(($) => $['operation.save'], { ns: 'common' })}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default AnnotationModal
