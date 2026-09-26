import PrimaryButton from '../components/PrimaryButton.jsx'
import { NavigationIcon, ClockIcon, SparkleRouteIcon } from '../components/icons.jsx'
import styles from './OnboardingScreen.module.css'

const FEATURES = [
  {
    icon: ClockIcon,
    title: 'Точно вовремя',
    text: 'Маршруты строго от 1 до 4 часов с запасом времени',
  },
  {
    icon: SparkleRouteIcon,
    title: 'Справка по пути',
    text: 'История и медиа открываются при приближении к объекту',
  },
]

export default function OnboardingScreen({ onStart }) {
  return (
    <div className={`screen ${styles.screen}`}>
      <div className="screen__body">
        <div className={styles.hero}>
          <div className={styles.iconWrap}>
            <NavigationIcon className={styles.icon} />
          </div>
          <h1 className={styles.title}>Преврати свободное время в готовую прогулку</h1>
          <p className={styles.subtitle}>
            Укажи доступное время и интересы — мы рассчитаем оптимальный пешеходный маршрут по
            центру Ростова без лишнего планирования.
          </p>
        </div>

        <div className={styles.features}>
          {FEATURES.map(({ icon: Icon, title, text }, index) => (
            <div
              className={styles.card}
              key={title}
              style={{ animationDelay: `${0.24 + index * 0.09}s` }}
            >
              <div className={styles.cardIcon}>
                <Icon />
              </div>
              <div className={styles.cardBody}>
                <h2 className={styles.cardTitle}>{title}</h2>
                <p className={styles.cardText}>{text}</p>
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className={styles.footer}>
        <PrimaryButton onClick={onStart}>Начать</PrimaryButton>
      </div>
    </div>
  )
}
