'use client'
import type { FC } from 'react'
import type { AlertDialogActions } from '@langgenius/dify-ui/alert-dialog'
import {
  AlertDialog,
  AlertDialogCancelButton,
  AlertDialogConfirmButton,
  AlertDialogContent,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@langgenius/dify-ui/alert-dialog'
import { Button } from '@langgenius/dify-ui/button'
import { IconButton } from '@langgenius/dify-ui/icon-button'
import { Pagination } from '@langgenius/dify-ui/pagination'
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
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useDebounce } from 'ahooks'
import { useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { SearchInput } from '@/app/components/base/search-input'
import { LoadingPlaceholder } from '@/app/components/base/loading-placeholder'
import { toast } from '@/app/notifications'
import { APP_PAGE_LIMIT } from '@/config'
import {
  deleteDatasetAnnotation,
  exportDatasetAnnotationsBlob,
  fetchDatasetAnnotations,
  type DatasetAnnotation,
  type DatasetAnnotationFeedbackType,
} from '@/service/dataset-annotation'
import { downloadBlob } from '@/utils/download'
import { formatTime } from '@/utils/time'
import AnnotationModal from './annotation-modal'

type AnnotationPageProps = {
  datasetId: string
}

const FEEDBACK_OPTIONS = [
  { value: 'none', labelKey: 'feedbackType.none' },
  { value: 'like', labelKey: 'feedbackType.like' },
  { value: 'dislike', labelKey: 'feedbackType.dislike' },
] as const

const toTimestamp = (date: string, endOfDay = false) => {
  if (!date) return undefined
  const parsed = new Date(`${date}T${endOfDay ? '23:59:59' : '00:00:00'}`)
  if (Number.isNaN(parsed.getTime())) return undefined
  return Math.floor(parsed.getTime() / 1000)
}

function FeedbackTypeBadge({ feedbackType }: { feedbackType: DatasetAnnotationFeedbackType | null }) {
  const { t } = useTranslation(['datasetAnnotations'])
  if (!feedbackType) return <span className="text-text-tertiary">-</span>
  const key = feedbackType === 'like' ? 'feedbackType.like' : 'feedbackType.dislike'
  return <span className="text-text-secondary">{t(($) => $[key], { ns: 'datasetAnnotations' })}</span>
}

const AnnotationPage: FC<AnnotationPageProps> = ({ datasetId }) => {
  const { t } = useTranslation(['datasetAnnotations', 'common'])
  const queryClient = useQueryClient()
  const [currPage, setCurrPage] = useState(0)
  const [limit, setLimit] = useState(APP_PAGE_LIMIT)
  const [tag, setTag] = useState('')
  const [feedbackType, setFeedbackType] = useState('none')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')
  const [editingAnnotation, setEditingAnnotation] = useState<DatasetAnnotation | null>(null)
  const [isShowCreate, setIsShowCreate] = useState(false)
  const [exportingType, setExportingType] = useState<'sft' | 'eval' | null>(null)
  const debouncedTag = useDebounce(tag, { wait: 500 })

  const queryParams = useMemo(
    () => ({
      page: currPage + 1,
      limit,
      tag: debouncedTag.trim() || undefined,
      feedback_type: feedbackType === 'none' ? undefined : (feedbackType as DatasetAnnotationFeedbackType),
      start_time: toTimestamp(startDate),
      end_time: toTimestamp(endDate, true),
    }),
    [currPage, limit, debouncedTag, feedbackType, startDate, endDate],
  )

  const { data, isLoading } = useQuery({
    queryKey: ['dataset-annotations', datasetId, queryParams],
    queryFn: () => fetchDatasetAnnotations(datasetId, queryParams),
  })

  const list = data?.data ?? []
  const total = data?.total ?? 0
  const totalPages = total ? Math.max(Math.ceil(total / limit), 1) : 1

  const invalidateList = () =>
    queryClient.invalidateQueries({ queryKey: ['dataset-annotations', datasetId] })

  const handleSaved = () => {
    invalidateList()
  }

  const handleExport = async (type: 'sft' | 'eval') => {
    setExportingType(type)
    try {
      const blob = await exportDatasetAnnotationsBlob(datasetId, type)
      downloadBlob({
        data: blob,
        fileName: `dataset_${datasetId}_annotations.${type === 'sft' ? 'jsonl' : 'json'}`,
      })
    } catch {
      toast.error(t(($) => $['export.failed'], { ns: 'datasetAnnotations' }))
    } finally {
      setExportingType(null)
    }
  }

  return (
    <div className="flex h-full flex-col px-6 pt-3">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 pb-2">
        <div>
          <h1 className="system-xl-semibold text-text-primary">
            {t(($) => $['title'], { ns: 'datasetAnnotations' })}
          </h1>
          <p className="system-sm-regular text-text-tertiary">
            {t(($) => $['description'], { ns: 'datasetAnnotations' })}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            size="small"
            loading={exportingType === 'sft'}
            disabled={exportingType === 'eval'}
            onClick={() => handleExport('sft')}
          >
            {t(($) => $['export.sft'], { ns: 'datasetAnnotations' })}
          </Button>
          <Button
            variant="secondary"
            size="small"
            loading={exportingType === 'eval'}
            disabled={exportingType === 'sft'}
            onClick={() => handleExport('eval')}
          >
            {t(($) => $['export.eval'], { ns: 'datasetAnnotations' })}
          </Button>
          <Button variant="primary" size="small" onClick={() => setIsShowCreate(true)}>
            {t(($) => $['create.title'], { ns: 'datasetAnnotations' })}
          </Button>
        </div>
      </div>

      <div className="flex shrink-0 flex-wrap items-center gap-2 pb-2">
        <SearchInput
          className="w-56"
          value={tag}
          placeholder={t(($) => $['filter.tag'], { ns: 'datasetAnnotations' })}
          onValueChange={setTag}
        />
        <Select<string>
          value={feedbackType}
          onValueChange={(nextValue) => setFeedbackType(nextValue ?? 'none')}
        >
          <SelectTrigger className="w-fit min-w-36">
            <SelectValue>
              {t(
                ($) =>
                  $[FEEDBACK_OPTIONS.find((option) => option.value === feedbackType)?.labelKey ?? 'feedbackType.none'],
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
        <label className="flex items-center gap-1.5 text-text-tertiary">
          <span className="system-sm-regular">{t(($) => $['filter.startTime'], { ns: 'datasetAnnotations' })}</span>
          <input
            type="date"
            value={startDate}
            onChange={(event) => setStartDate(event.target.value)}
            className="rounded-lg border border-divider-subtle bg-components-input-bg-normal px-2 py-1 text-text-primary"
          />
        </label>
        <label className="flex items-center gap-1.5 text-text-tertiary">
          <span className="system-sm-regular">{t(($) => $['filter.endTime'], { ns: 'datasetAnnotations' })}</span>
          <input
            type="date"
            value={endDate}
            onChange={(event) => setEndDate(event.target.value)}
            className="rounded-lg border border-divider-subtle bg-components-input-bg-normal px-2 py-1 text-text-primary"
          />
        </label>
      </div>

      {isLoading ? (
        <LoadingPlaceholder className="h-full" />
      ) : list.length === 0 ? (
        <div className="flex h-full items-center justify-center text-sm text-text-tertiary">
          {t(($) => $['empty'], { ns: 'datasetAnnotations' })}
        </div>
      ) : (
        <div className="min-h-0 grow overflow-x-auto rounded-lg border border-divider-subtle">
          <table className="w-full min-w-160 border-collapse">
            <thead className="system-xs-medium-uppercase text-text-tertiary">
              <tr className="bg-background-section-burn">
                <th className="w-2/5 px-3 py-2 text-left font-[weight:inherit] whitespace-nowrap">
                  {t(($) => $['table.question'], { ns: 'datasetAnnotations' })}
                </th>
                <th className="px-3 py-2 text-left font-[weight:inherit] whitespace-nowrap">
                  {t(($) => $['table.answer'], { ns: 'datasetAnnotations' })}
                </th>
                <th className="px-3 py-2 text-left font-[weight:inherit] whitespace-nowrap">
                  {t(($) => $['table.tags'], { ns: 'datasetAnnotations' })}
                </th>
                <th className="px-3 py-2 text-left font-[weight:inherit] whitespace-nowrap">
                  {t(($) => $['table.feedbackType'], { ns: 'datasetAnnotations' })}
                </th>
                <th className="px-3 py-2 text-left font-[weight:inherit] whitespace-nowrap">
                  {t(($) => $['table.createdAt'], { ns: 'datasetAnnotations' })}
                </th>
                <th className="w-24 px-3 py-2 text-left font-[weight:inherit] whitespace-nowrap">
                  {t(($) => $['table.actions'], { ns: 'datasetAnnotations' })}
                </th>
              </tr>
            </thead>
            <tbody className="system-sm-regular text-text-secondary">
              {list.map((item) => (
                <AnnotationRow
                  key={item.id}
                  datasetId={datasetId}
                  item={item}
                  onEdit={() => setEditingAnnotation(item)}
                  onRemoved={handleSaved}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {total > limit && (
        <div className="shrink-0 py-2">
          <Pagination
            page={currPage + 1}
            totalPages={totalPages}
            onPageChange={(page) => setCurrPage(page - 1)}
            labels={{
              previous: t(($) => $['pagination.previous'], { ns: 'common' }),
              next: t(($) => $['pagination.next'], { ns: 'common' }),
              editPageNumber: (page, totalPages) =>
                t(($) => $['pagination.editPageNumber'], { ns: 'common', page, totalPages }),
              pageNumberInput: t(($) => $['pagination.pageNumber'], { ns: 'common' }),
            }}
            pageSize={{
              value: limit,
              options: [10, 25, 50],
              onValueChange: setLimit,
              label: t(($) => $['pagination.perPage'], { ns: 'common' }),
              ariaLabel: t(($) => $['pagination.perPage'], { ns: 'common' }),
            }}
          />
        </div>
      )}

      <AnnotationModal
        isShow={isShowCreate}
        onHide={() => setIsShowCreate(false)}
        datasetId={datasetId}
        onSaved={handleSaved}
      />
      <AnnotationModal
        isShow={!!editingAnnotation}
        onHide={() => setEditingAnnotation(null)}
        datasetId={datasetId}
        annotation={editingAnnotation}
        onSaved={handleSaved}
      />
    </div>
  )
}

type AnnotationRowProps = {
  datasetId: string
  item: DatasetAnnotation
  onEdit: () => void
  onRemoved: () => void
}

function AnnotationRow({ datasetId, item, onEdit, onRemoved }: AnnotationRowProps) {
  const { t } = useTranslation(['datasetAnnotations', 'common'])
  const removeActionsRef = useRef<AlertDialogActions>(null)
  const { mutateAsync: removeAnnotation, isPending } = useMutation({
    mutationFn: () => deleteDatasetAnnotation(datasetId, item.id),
    onSuccess: () => {
      removeActionsRef.current?.close()
      toast.success(t(($) => $['api.actionSuccess'], { ns: 'common' }))
      onRemoved()
    },
    onError: () => {
      toast.error(t(($) => $['api.actionFailed'], { ns: 'common' }))
    },
  })

  return (
    <tr className="border-b border-divider-subtle align-middle hover:bg-background-default-hover">
      <td className="max-w-80 truncate px-3 py-2" title={item.question}>
        {item.question}
      </td>
      <td className="max-w-80 truncate px-3 py-2" title={item.answer}>
        {item.answer}
      </td>
      <td className="max-w-40 px-3 py-2">
        <div className="flex flex-wrap gap-1">
          {item.tags?.length ? (
            item.tags.map((tag) => (
              <span
                key={tag}
                className="rounded-md border border-divider-deep bg-components-badge-white-to-dark px-1.5 py-0.5 text-xs text-text-secondary"
              >
                {tag}
              </span>
            ))
          ) : (
            <span className="text-text-tertiary">-</span>
          )}
        </div>
      </td>
      <td className="px-3 py-2">
        <FeedbackTypeBadge feedbackType={item.feedback_type} />
      </td>
      <td className="whitespace-nowrap px-3 py-2">
        {item.created_at != null
          ? formatTime({ date: item.created_at * 1000, dateFormat: 'YYYY-MM-DD HH:mm' })
          : '-'}
      </td>
      <td className="px-3 py-2">
        <div className="flex space-x-1 text-text-tertiary">
          <IconButton
            aria-label={t(($) => $['table.edit'], { ns: 'datasetAnnotations' })}
            onClick={onEdit}
          >
            <span aria-hidden className="i-ri-edit-line size-4" />
          </IconButton>
          <AlertDialog
            actionsRef={removeActionsRef}
            onOpenChange={(open, details) => {
              if (!open && isPending && details.reason !== 'imperative-action') details.cancel()
            }}
          >
            <AlertDialogTrigger
              render={
                <IconButton
                  aria-label={t(($) => $['table.delete'], { ns: 'datasetAnnotations' })}
                >
                  <span aria-hidden className="i-ri-delete-bin-line size-4" />
                </IconButton>
              }
            />
            <RemoveAnnotationConfirmContent
              isPending={isPending}
              onRemove={() => void removeAnnotation()}
            />
          </AlertDialog>
        </div>
      </td>
    </tr>
  )
}

function RemoveAnnotationConfirmContent({
  isPending,
  onRemove,
}: {
  isPending: boolean
  onRemove: () => void
}) {
  const { t } = useTranslation(['datasetAnnotations', 'common'])
  return (
    <AlertDialogContent>
      <AlertDialogHeader>
        <AlertDialogTitle>{t(($) => $['delete.confirm'], { ns: 'datasetAnnotations' })}</AlertDialogTitle>
      </AlertDialogHeader>
      <AlertDialogFooter>
        <AlertDialogCancelButton disabled={isPending}>
          {t(($) => $['operation.cancel'], { ns: 'common' })}
        </AlertDialogCancelButton>
        <AlertDialogConfirmButton loading={isPending} onClick={onRemove}>
          {t(($) => $['operation.confirm'], { ns: 'common' })}
        </AlertDialogConfirmButton>
      </AlertDialogFooter>
    </AlertDialogContent>
  )
}

export default AnnotationPage
