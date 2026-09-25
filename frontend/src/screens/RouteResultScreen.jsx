import PrimaryButton from '../components/PrimaryButton.jsx'
import { ArrowLeftIcon, ClockIcon, CoinIcon, PinIcon, StarIcon, WalkIcon } from '../components/icons.jsx'
import { formatMinutes, formatPrice } from '../lib/format.js'
import styles from './RouteResultScreen.module.css'

function StopCard({ stop, index }) {
  const { place } = stop
  return (
    <li className={styles.stop} style={{ animationDelay: `${0.1 + index * 0.07}s` }}>
      <div className={styles.timeline}>
        <span className={styles.order}>{stop.order}</span>
        {index !== 0 && (
          <span className={styles.travel}>
            <WalkIcon />
            {formatMinutes(stop.travel_minutes_from_prev)}
          </span>
        )}
      </div>
      <div className={styles.stopBody}>
        <div className={styles.stopHead}>
          <h3 className={styles.stopTitle}>{place.title}</h3>
          <span className={styles.rating}>
            <StarIcon />
            {place.rating.toFixed(1)}
          </span>
        </div>
        {place.description && <p className={styles.stopDesc}>{place.description}</p>}
        <div className={styles.meta}>
          <span className={styles.metaItem}>
            <ClockIcon />
            {formatMinutes(stop.visit_duration_minutes)}
          </span>
          <span className={styles.metaItem}>
            <CoinIcon />
            {formatPrice(place.price)}
          </span>
          {place.address && (
            <span className={styles.metaItem}>
              <PinIcon />
              {place.address}
            </span>
          )}
        </div>
        <p className={styles.arrival}>Прибытие через {formatMinutes(stop.arrival_offset_minutes)}</p>
      </div>
    </li>
  )
}

export default function RouteResultScreen({ route, onEdit, onRestart }) {
  const totalMinutes = Math.round(route.total_duration_hours * 60)

  return (
    <div className={`screen ${styles.screen}`}>
      <header className={styles.header}>
        <button type="button" className={styles.back} onClick={onEdit} aria-label="Изменить параметры">
          <ArrowLeftIcon />
        </button>
        <h1 className={styles.headerTitle}>{route.title}</h1>
      </header>

      <div className={styles.summary}>
        <div className={styles.stat}>
          <span className={styles.statValue}>{formatMinutes(totalMinutes)}</span>
          <span className={styles.statLabel}>в пути</span>
        </div>
        <div className={styles.stat}>
          <span className={styles.statValue}>{route.stops.length}</span>
          <span className={styles.statLabel}>точек</span>
        </div>
        <div className={styles.stat}>
          <span className={styles.statValue}>{formatPrice(route.total_cost)}</span>
          <span className={styles.statLabel}>бюджет</span>
        </div>
      </div>

      {route.stops.length === 0 ? (
        <p className={styles.empty}>Маршрут пуст — попробуйте изменить время или интересы.</p>
      ) : (
        <ol className={styles.stops}>
          {route.stops.map((stop, index) => (
            <StopCard key={stop.place.id} stop={stop} index={index} />
          ))}
        </ol>
      )}

      <div className={styles.footer}>
        <PrimaryButton onClick={onRestart}>Хочу новую прогулку</PrimaryButton>
      </div>
    </div>
  )
}
