'use client'
import type { FC } from 'react'
import type {
  DocumentVersionResponse,
} from '@dify/contracts/api/console/datasets/types.gen'
import { Button } from '@langgenius/dify-ui/button'
import { cn } from '@langgenius/dify-ui/cn'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { LoadingPlaceholder } from '@/app/components/base/loading-placeholder'
import { toast } from '@/app/notifications'
import { consoleQuery } from '@/service/console'
import { formatTime } from '@/utils/time'

type VersionHistoryProps = {
  datasetId: string
  documentId: string
}

const VersionHistory: FC<VersionHistoryProps> = ({ datasetId, documentId }) => {
  const { t } = useTranslation(['datasetDocuments'])
  const queryClient = useQueryClient()

  const versionsQueryOptions = useMemo(
    () =>
      consoleQuery.datasets.byDatasetId.documents.byDocumentId.versions.get.queryOptions({
        input: { params: { dataset_id: datasetId, document_id: documentId } },
      }),
    [datasetId, documentId],
  )
  const { data: versionsData, isLoading } = useQuery(versionsQueryOptions)
  const versions = useMemo(() => versionsData?.versions ?? [], [versionsData])

  const [selectedVersionId, setSelectedVersionId] = useState<string | undefined>(undefined)

  useEffect(() => {
    if (selectedVersionId) return
    const latest = versions.find((version) => version.is_latest) ?? versions[0]
    if (latest) setSelectedVersionId(latest.id)
  }, [versions, selectedVersionId])

  const selectedVersion = useMemo(
    () => versions.find((version) => version.id === selectedVersionId),
    [versions, selectedVersionId],
  )

  const detailQueryOptions = useMemo(
    () =>
      consoleQuery.datasets.byDatasetId.documents.byDocumentId.versions.byVersionId.get.queryOptions(
        {
          input: {
            params: {
              dataset_id: datasetId,
              document_id: documentId,
              version_id: selectedVersionId ?? '',
            },
          },
        },
      ),
    [datasetId, documentId, selectedVersionId],
  )
  const { data: detailData, isLoading: isDetailLoading } = useQuery({
    ...detailQueryOptions,
    enabled: !!selectedVersionId,
  })

  const invalidateVersions = () => {
    queryClient.invalidateQueries({
      queryKey: consoleQuery.datasets.byDatasetId.documents.byDocumentId.versions.get.key({
        input: { params: { dataset_id: datasetId, document_id: documentId } },
      }),
    })
  }

  const { mutateAsync: updateArchived, isPending: isArchiving } = useMutation(
    consoleQuery.datasets.byDatasetId.documents.byDocumentId.versions.byVersionId.archive.post.mutationOptions(),
  )

  const handleArchiveToggle = async (version: DocumentVersionResponse) => {
    const nextArchived = !version.archived
    try {
      await updateArchived({
        params: {
          dataset_id: datasetId,
          document_id: documentId,
          version_id: version.id,
        },
        body: { archived: nextArchived },
      })
      toast.success(
        t(
          ($) =>
            $[nextArchived ? 'versionHistory.archiveSuccess' : 'versionHistory.unarchiveSuccess'],
          { ns: 'datasetDocuments' },
        ),
      )
      invalidateVersions()
    } catch {
      toast.error(t(($) => $['versionHistory.archiveFailed'], { ns: 'datasetDocuments' }))
    }
  }

  if (isLoading) {
    return <LoadingPlaceholder className="h-full" />
  }

  if (versions.length === 0) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 p-4">
        <div className="text-sm font-medium text-text-secondary">
          {t(($) => $['versionHistory.empty'], { ns: 'datasetDocuments' })}
        </div>
        <div className="text-xs text-text-tertiary">
          {t(($) => $['versionHistory.emptyHint'], { ns: 'datasetDocuments' })}
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0">
      <div className="flex w-80 shrink-0 flex-col border-r border-divider-subtle">
        <div className="border-b border-divider-subtle px-4 py-3 text-sm font-semibold text-text-secondary">
          {t(($) => $['versionHistory.tab'], { ns: 'datasetDocuments' })}
        </div>
        <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto p-2">
          {versions.map((version) => {
            const isSelected = version.id === selectedVersionId
            return (
              <button
                key={version.id}
                type="button"
                onClick={() => setSelectedVersionId(version.id)}
                aria-pressed={isSelected}
                className={cn(
                  'flex w-full flex-col gap-1 rounded-lg border border-transparent px-3 py-2 text-left transition-colors',
                  'focus-visible:outline-1 focus-visible:outline-components-input-border-active',
                  isSelected
                    ? 'border-components-panel-border bg-components-panel-bg-blur'
                    : 'hover:bg-components-button-tertiary-bg',
                )}
              >
                <div className="flex items-center gap-2">
                  <span className="text-sm font-semibold text-text-primary">
                    v{version.version}
                  </span>
                  {version.is_latest && (
                    <span className="rounded border border-util-colors-green-green-100 bg-util-colors-green-green-50 px-1 py-0.5 text-[10px] font-medium text-util-colors-green-green-700">
                      {t(($) => $['versionHistory.latest'], { ns: 'datasetDocuments' })}
                    </span>
                  )}
                  {version.archived && (
                    <span className="rounded border border-util-colors-gray-gray-100 bg-util-colors-gray-gray-50 px-1 py-0.5 text-[10px] font-medium text-text-tertiary">
                      {t(($) => $['versionHistory.archived'], { ns: 'datasetDocuments' })}
                    </span>
                  )}
                </div>
                <div className="truncate text-xs text-text-secondary">{version.file_name}</div>
                <div className="text-xs text-text-tertiary">
                  {formatTime({
                    date: version.created_at * 1000,
                    dateFormat: 'YYYY-MM-DD HH:mm',
                  })}
                </div>
              </button>
            )
          })}
        </div>
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        {selectedVersion ? (
          <>
            <div className="flex items-center justify-between gap-3 border-b border-divider-subtle px-4 py-3">
              <div className="min-w-0">
                <div className="text-sm font-semibold text-text-primary">
                  {t(($) => $['versionHistory.preview'], { ns: 'datasetDocuments' })} · v{selectedVersion.version}
                </div>
                <div className="mt-0.5 flex flex-wrap gap-x-3 text-xs text-text-tertiary">
                  <span>{selectedVersion.file_name}</span>
                  {selectedVersion.word_count != null && (
                    <span>
                      {t(($) => $['versionHistory.wordCount'], { ns: 'datasetDocuments' })}{' '}
                      {selectedVersion.word_count}
                    </span>
                  )}
                  {selectedVersion.file_hash && (
                    <span className="truncate" title={selectedVersion.file_hash}>
                      SHA-256: {selectedVersion.file_hash}
                    </span>
                  )}
                </div>
              </div>
              {!selectedVersion.is_latest && (
                <Button
                  variant="secondary"
                  size="small"
                  loading={isArchiving}
                  onClick={() => handleArchiveToggle(selectedVersion)}
                >
                  {selectedVersion.archived
                    ? t(($) => $['versionHistory.unarchive'], { ns: 'datasetDocuments' })
                    : t(($) => $['versionHistory.archive'], { ns: 'datasetDocuments' })}
                </Button>
              )}
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              {isDetailLoading ? (
                <div className="text-sm text-text-tertiary">...</div>
              ) : (
                <pre className="whitespace-pre-wrap font-sans text-sm leading-6 text-text-primary">
                  {detailData?.content || ''}
                </pre>
              )}
            </div>
          </>
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-text-tertiary">
            {t(($) => $['versionHistory.selectToPreview'], { ns: 'datasetDocuments' })}
          </div>
        )}
      </div>
    </div>
  )
}

export default VersionHistory