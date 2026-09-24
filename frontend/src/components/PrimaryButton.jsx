import styles from './PrimaryButton.module.css'
import { hapticTap } from '../lib/messenger.js'

export default function PrimaryButton({ children, onClick, disabled, type = 'button', className }) {
  return (
    <button
      type={type}
      className={`${styles.button} ${className || ''}`}
      disabled={disabled}
      onClick={(event) => {
        hapticTap()
        onClick?.(event)
      }}
    >
      {children}
    </button>
  )
}
